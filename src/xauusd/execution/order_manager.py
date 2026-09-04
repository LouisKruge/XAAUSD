"""Order manager: the only component that sends orders.

The hard problem it exists to solve is not placing an order — it is knowing what
happened when the answer is unclear. Three rules:

  1. The client tag is DETERMINISTIC and is written to the database as INTENT before
     any network call. The same setup always produces the same tag.
  2. An ambiguous send is NEVER resent. It enters RECONCILING and the broker's own
     position and deal history decide, by tag.
  3. If ground truth cannot be established within the timeout, the kill switch trips
     and a human is alerted. An unknown order state is a stop-everything condition, not
     something to retry through.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xauusd.config.settings import Settings
from xauusd.domain.enums import (
    KillSwitchReason,
    OrderStatus,
)
from xauusd.domain.types import (
    BrokerPosition,
    Decision,
    OrderRequest,
    Quote,
    SymbolSpec,
    TradePlan,
)
from xauusd.execution.broker import AmbiguousSendError, Broker, BrokerError
from xauusd.execution.retcodes import RetAction, classify
from xauusd.monitoring.alerts import Notifier
from xauusd.monitoring.logging import get_logger
from xauusd.risk.kill_switch import KillSwitch

log = get_logger(__name__)


@dataclass(slots=True)
class ExecutionOutcome:
    ok: bool
    status: OrderStatus
    client_tag: str
    ticket: int | None = None
    fill_price: float = 0.0
    volume: float = 0.0
    attempts: int = 0
    reason: str = ""
    reconciled: bool = False
    history: list[str] = field(default_factory=list)

    def log_line(self) -> str:
        return f"{self.client_tag} {self.status} after {self.attempts} attempt(s): {self.reason}"


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        settings: Settings,
        kill_switch: KillSwitch,
        notifier: Notifier | None = None,
        persist: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.broker = broker
        self.settings = settings
        self.kill_switch = kill_switch
        self.notifier = notifier or Notifier()
        self.persist = persist or (lambda tag, status, data: None)

    # -- pre-send ----------------------------------------------------------------------

    def preflight(
        self,
        plan: TradePlan,
        lots: float,
        spec: SymbolSpec,
        quote: Quote,
        now: datetime,
        classification: object | None = None,
    ) -> tuple[bool, str, TradePlan]:
        """Re-derive everything at send time. Nothing from the analysis cycle is trusted.

        Returns (ok, reason, repriced_plan). The plan comes back REPRICED at the live
        quote with every RR recomputed, so the RR check below sees the trade that would
        actually be entered rather than the one that was analysed seconds ago.
        """
        e = self.settings.execution

        age = (now - quote.ts).total_seconds()
        if age > e.max_quote_age_seconds:
            return False, f"quote is {age:.1f}s old (limit {e.max_quote_age_seconds}s)", plan

        spread_pts = quote.spread_points(spec.point)
        if spread_pts > e.max_spread_points:
            return False, f"spread {spread_pts:.0f}pts exceeds {e.max_spread_points}", plan

        entry_now = quote.price_for(plan.direction)
        drift_r = abs(entry_now - plan.entry) / plan.risk_distance if plan.risk_distance else 1.0
        if drift_r > e.max_entry_drift_r:
            return (
                False,
                (
                    f"price drifted {drift_r:.2f}R from the signal price; abandoning rather "
                    f"than chasing"
                ),
                plan,
            )

        repriced = plan.with_entry(entry_now)

        # Normalise to the broker's tick grid FIRST, then re-check RR: rounding can
        # push a marginal 2.01 RR below the floor and that must be caught here.
        sl = spec.normalize_price(repriced.stop_loss)
        tp = spec.normalize_price(repriced.final_target.price)
        entry_n = spec.normalize_price(entry_now)
        dist = abs(entry_n - sl)
        if dist <= spec.stops_level_price:
            return (
                False,
                (
                    f"stop distance {dist:.5f} is inside the broker's stops_level "
                    f"{spec.stops_level_price:.5f}"
                ),
                repriced,
            )
        if abs(tp - entry_n) <= spec.stops_level_price:
            return False, "take profit is inside the broker's stops_level", repriced

        rr_after = abs(tp - entry_n) / dist if dist > 0 else 0.0
        floor = self.settings.min_rr_for(classification)
        if rr_after < floor:
            return (
                False,
                (
                    f"reward-to-risk fell to {rr_after:.2f} after repricing and rounding "
                    f"(floor {floor}); abandoning the setup"
                ),
                repriced,
            )

        if lots < spec.volume_min:
            return False, f"lots {lots} below the broker minimum {spec.volume_min}", repriced

        return True, "ok", repriced

    # -- send --------------------------------------------------------------------------

    def execute(
        self,
        decision: Decision,
        client_tag: str,
        spec: SymbolSpec,
        now: datetime,
        magic: int | None = None,
    ) -> ExecutionOutcome:
        plan = decision.plan
        sizing = decision.sizing
        if plan is None or sizing is None or not sizing.approved:
            return ExecutionOutcome(
                False, OrderStatus.ABANDONED, client_tag, reason="no approved plan or sizing"
            )

        magic = magic if magic is not None else self.settings.broker.magic
        history: list[str] = []

        # Duplicate guard against the BROKER, not against our memory.
        try:
            existing = [
                p
                for p in self.broker.positions(magic=magic)
                if client_tag and client_tag in (p.comment or "")
            ]
        except BrokerError as exc:
            return ExecutionOutcome(
                False,
                OrderStatus.ABANDONED,
                client_tag,
                reason=f"cannot verify existing positions: {exc}",
            )
        if existing:
            return ExecutionOutcome(
                False,
                OrderStatus.ABANDONED,
                client_tag,
                ticket=existing[0].ticket,
                reason="a position for this exact setup already exists",
            )

        self.persist(
            client_tag,
            str(OrderStatus.INTENT),
            {
                "strategy": plan.strategy,
                "direction": str(plan.direction),
                "lots": sizing.lots,
                "entry": plan.entry,
                "sl": plan.stop_loss,
            },
        )

        max_attempts = self.settings.execution.max_send_retries + 1
        last_reason = ""
        for attempt in range(1, max_attempts + 1):
            try:
                quote = self.broker.quote(spec.symbol)
            except BrokerError as exc:
                last_reason = f"cannot fetch quote: {exc}"
                history.append(last_reason)
                break

            ok, reason, repriced = self.preflight(
                plan, sizing.lots, spec, quote, now, decision.classification
            )
            if not ok:
                history.append(f"attempt {attempt}: preflight rejected — {reason}")
                self.persist(client_tag, str(OrderStatus.ABANDONED), {"reason": reason})
                return ExecutionOutcome(
                    False,
                    OrderStatus.ABANDONED,
                    client_tag,
                    attempts=attempt,
                    reason=reason,
                    history=history,
                )

            req = OrderRequest(
                symbol=spec.symbol,
                direction=repriced.direction,
                volume=sizing.lots,
                price=spec.normalize_price(quote.price_for(repriced.direction)),
                stop_loss=spec.normalize_price(repriced.stop_loss),
                take_profit=spec.normalize_price(repriced.final_target.price),
                client_tag=client_tag,
                magic=magic,
                comment=f"{plan.strategy[:12]}:{client_tag}",
                max_slippage_points=self.settings.execution.max_slippage_points,
            )
            self.persist(client_tag, str(OrderStatus.SENT), {"attempt": attempt})

            try:
                result = self.broker.send_market(req)
            except AmbiguousSendError as exc:
                history.append(f"attempt {attempt}: AMBIGUOUS — {exc}")
                return self._reconcile(client_tag, magic, spec, attempt, history)

            if result.status is OrderStatus.RECONCILING:
                history.append(f"attempt {attempt}: ambiguous — {result.retcode_text}")
                return self._reconcile(client_tag, magic, spec, attempt, history)

            if result.ok:
                self.persist(
                    client_tag,
                    str(OrderStatus.FILLED),
                    {
                        "ticket": result.ticket,
                        "price": result.fill_price,
                        "volume": result.filled_volume,
                    },
                )
                log.info(
                    "order_filled",
                    tag=client_tag,
                    ticket=result.ticket,
                    price=result.fill_price,
                    volume=result.filled_volume,
                    strategy=plan.strategy,
                    classification=str(decision.classification),
                )
                return ExecutionOutcome(
                    True,
                    OrderStatus.FILLED,
                    client_tag,
                    result.ticket,
                    result.fill_price,
                    result.filled_volume,
                    attempt,
                    "filled",
                    history=history,
                )

            action, description = classify(result.retcode)
            last_reason = f"{result.retcode} {description}"
            history.append(f"attempt {attempt}: {last_reason} -> {action}")
            log.warning(
                "order_rejected",
                tag=client_tag,
                retcode=result.retcode,
                action=str(action),
                detail=description,
            )

            if action is RetAction.SUCCESS:
                continue
            if action in (RetAction.RETRY_REPRICE, RetAction.FIX_AND_RETRY):
                if attempt >= max_attempts:
                    break
                time.sleep(0.25)
                continue
            if action is RetAction.RETRY_TRANSIENT:
                if attempt >= max_attempts:
                    break
                # RECONCILE BEFORE RETRYING: the previous attempt may have landed.
                found = self._find(client_tag, magic)
                if found is not None:
                    history.append("reconcile found the order did land; not resending")
                    return ExecutionOutcome(
                        True,
                        OrderStatus.FILLED,
                        client_tag,
                        found.ticket,
                        found.entry_price,
                        found.volume,
                        attempt,
                        "recovered by reconciliation",
                        reconciled=True,
                        history=history,
                    )
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            if action is RetAction.ABORT_AND_ALERT:
                self.notifier.error(
                    "EXECUTION",
                    f"Order aborted: {description}",
                    tag=client_tag,
                    retcode=result.retcode,
                )
                break
            if action in (RetAction.KILL_SWITCH, RetAction.UNKNOWN):
                self.kill_switch.trip(
                    KillSwitchReason.SYSTEM_ERROR,
                    f"order return code {result.retcode}: {description}",
                    {"tag": client_tag},
                    now,
                )
                break
            break

        self.persist(client_tag, str(OrderStatus.ABANDONED), {"reason": last_reason})
        return ExecutionOutcome(
            False,
            OrderStatus.ABANDONED,
            client_tag,
            attempts=max_attempts,
            reason=last_reason or "rejected",
            history=history,
        )

    # -- reconciliation ----------------------------------------------------------------

    def _find(self, client_tag: str, magic: int) -> BrokerPosition | None:
        finder = getattr(self.broker, "find_by_tag", None)
        if finder is not None:
            try:
                found: BrokerPosition | None = finder(client_tag)
            except BrokerError:
                return None
            return found
        try:
            for p in self.broker.positions(magic=magic):
                if client_tag and client_tag in (p.comment or ""):
                    return p
        except BrokerError:
            return None
        return None

    def _reconcile(
        self,
        client_tag: str,
        magic: int,
        spec: SymbolSpec,
        attempt: int,
        history: list[str],
    ) -> ExecutionOutcome:
        """Establish ground truth from the BROKER after an ambiguous send.

        Never resends. Either the order exists at the broker or it does not, and only
        the broker can say which.
        """
        self.persist(client_tag, str(OrderStatus.RECONCILING), {})
        deadline = time.monotonic() + self.settings.execution.reconcile_timeout_seconds
        poll = 0
        while time.monotonic() < deadline:
            poll += 1
            found = self._find(client_tag, magic)
            if found is not None:
                history.append(f"reconciled after {poll} polls: order DID land")
                self.persist(
                    client_tag,
                    str(OrderStatus.FILLED),
                    {
                        "ticket": found.ticket,
                        "price": found.entry_price,
                        "recovered": True,
                    },
                )
                log.warning(
                    "ambiguous_send_recovered",
                    tag=client_tag,
                    ticket=found.ticket,
                    detail="the order reached the server despite the failed response",
                )
                return ExecutionOutcome(
                    True,
                    OrderStatus.FILLED,
                    client_tag,
                    found.ticket,
                    found.entry_price,
                    found.volume,
                    attempt,
                    "recovered by reconciliation",
                    reconciled=True,
                    history=history,
                )
            time.sleep(1.0)

        # Could not establish ground truth. This is a stop-everything condition.
        history.append("reconciliation timed out: order state UNKNOWN")
        self.kill_switch.trip(
            KillSwitchReason.STATE_DIVERGENCE,
            f"order {client_tag} has an unknown state after an ambiguous send; "
            f"a human must verify the account before trading resumes",
            {"client_tag": client_tag},
        )
        self.notifier.critical(
            "EXECUTION",
            "Order state unknown — trading halted",
            f"Tag {client_tag} could not be reconciled. Check the terminal manually.",
            tag=client_tag,
        )
        self.persist(client_tag, str(OrderStatus.RECONCILING), {"unresolved": True})
        return ExecutionOutcome(
            False,
            OrderStatus.RECONCILING,
            client_tag,
            attempts=attempt,
            reason="unresolved after an ambiguous send",
            history=history,
        )

    # -- startup -----------------------------------------------------------------------

    def reconcile_unresolved(self, tags: list[str], magic: int | None = None) -> dict[str, str]:
        """Run at startup, before the engine is allowed to trade.

        Anything left INTENT/SENT/RECONCILING from a previous run is resolved against
        the broker first. Never assume a pre-crash view of positions is still true.
        """
        magic = magic if magic is not None else self.settings.broker.magic
        out: dict[str, str] = {}
        for tag in tags:
            found = self._find(tag, magic)
            if found is not None:
                out[tag] = f"FILLED ticket {found.ticket}"
                self.persist(
                    tag,
                    str(OrderStatus.FILLED),
                    {"ticket": found.ticket, "recovered_at_startup": True},
                )
            else:
                out[tag] = "NOT_FOUND (never reached the server)"
                self.persist(
                    tag,
                    str(OrderStatus.ABANDONED),
                    {"reason": "not found at broker during startup reconcile"},
                )
        if out:
            log.warning("startup_reconciliation", resolved=out)
        return out
