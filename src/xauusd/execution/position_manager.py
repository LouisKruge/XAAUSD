"""Position management after entry.

The stop loss lives on the BROKER at all times. A position without a server-side stop
is not permitted to persist, because a crashed engine must leave protected positions
rather than naked ones.

The one rule that is enforced structurally rather than by convention: `modify_stop`
asserts the new stop is not further from entry than the current one and refuses
otherwise. There is no code path in this class that can widen a stop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from xauusd.config.settings import ExecutionConfig, Settings
from xauusd.domain.enums import Direction, ExitReason
from xauusd.domain.types import (
    BrokerPosition,
    MarketSnapshot,
    Quote,
    SymbolSpec,
    TradePlan,
)
from xauusd.execution.broker import Broker, BrokerError
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


class StopWideningRefused(RuntimeError):
    """Raised on any attempt to move a stop further from entry.

    Never caught and downgraded. A caller trying to widen a stop is a bug, and the
    prohibition is one of the system's non-negotiables.
    """


@dataclass(slots=True)
class ManagedPosition:
    ticket: int
    plan: TradePlan
    entry: float
    initial_stop: float
    current_stop: float
    take_profit: float | None
    volume: float
    remaining: float
    opened_at: datetime
    direction: Direction
    bars_held: int = 0
    partial_taken: bool = False
    moved_to_be: bool = False
    mae_r: float = 0.0
    mfe_r: float = 0.0
    events: list[str] = field(default_factory=list)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.initial_stop)

    def r_at(self, price: float) -> float:
        return (price - self.entry) * self.direction.sign / self.risk if self.risk > 0 else 0.0

    def current_risk(self) -> float:
        """Distance from entry to the CURRENT stop. Negative once past break-even."""
        return (self.entry - self.current_stop) * self.direction.sign


@dataclass(slots=True)
class ManagementAction:
    ticket: int
    kind: str  # BREAK_EVEN | TRAIL | PARTIAL | TIME_STOP | INVALIDATION | CLOSE
    old_stop: float | None = None
    new_stop: float | None = None
    volume: float | None = None
    reason: str = ""
    applied: bool = False


class PositionManager:
    def __init__(
        self,
        broker: Broker,
        settings: Settings,
        on_event: Callable[[ManagementAction], None] | None = None,
    ) -> None:
        self.broker = broker
        self.settings = settings
        self.cfg: ExecutionConfig = settings.execution
        self.positions: dict[int, ManagedPosition] = {}
        self.on_event = on_event

    # -- registration ------------------------------------------------------------------

    def adopt(self, ticket: int, plan: TradePlan, position: BrokerPosition) -> ManagedPosition:
        mp = ManagedPosition(
            ticket=ticket,
            plan=plan,
            entry=position.entry_price,
            initial_stop=position.stop_loss or plan.stop_loss,
            current_stop=position.stop_loss or plan.stop_loss,
            take_profit=position.take_profit or plan.final_target.price,
            volume=position.volume,
            remaining=position.volume,
            opened_at=position.opened_at,
            direction=position.direction,
        )
        self.positions[ticket] = mp
        return mp

    def forget(self, ticket: int) -> None:
        self.positions.pop(ticket, None)

    # -- the stop invariant ------------------------------------------------------------

    def modify_stop(self, mp: ManagedPosition, new_stop: float, reason: str) -> ManagementAction:
        """Move a stop. REFUSES to widen — there is no path through this that can."""
        old = mp.current_stop
        old_risk = (mp.entry - old) * mp.direction.sign
        new_risk = (mp.entry - new_stop) * mp.direction.sign
        if new_risk > old_risk + 1e-9:
            raise StopWideningRefused(
                f"ticket {mp.ticket}: moving the stop from {old} to {new_stop} would "
                f"increase risk from {old_risk:.5f} to {new_risk:.5f}"
            )
        if abs(new_stop - old) < 1e-9:
            return ManagementAction(mp.ticket, "NOOP", old, new_stop, reason=reason)

        action = ManagementAction(
            mp.ticket, reason.split(":")[0].upper() or "TRAIL", old, new_stop, reason=reason
        )
        try:
            result = self.broker.modify_position(mp.ticket, sl=new_stop)
        except BrokerError as exc:
            action.reason = f"{reason} (broker error: {exc})"
            return action
        if result.ok:
            mp.current_stop = new_stop
            mp.events.append(f"{reason}: {old:.2f} -> {new_stop:.2f}")
            action.applied = True
            log.info("stop_moved", ticket=mp.ticket, old=old, new=new_stop, reason=reason)
        else:
            action.reason = f"{reason} (rejected: {result.retcode_text})"
        if self.on_event:
            self.on_event(action)
        return action

    # -- per-cycle management ----------------------------------------------------------

    def manage(
        self,
        quote: Quote,
        now: datetime,
        spec: SymbolSpec,
        snapshot: MarketSnapshot | None = None,
        bar_closed: bool = False,
    ) -> list[ManagementAction]:
        actions: list[ManagementAction] = []
        e = self.cfg

        for mp in list(self.positions.values()):
            price = quote.exit_price_for(mp.direction)
            r_now = mp.r_at(price)
            mp.mfe_r = max(mp.mfe_r, r_now)
            mp.mae_r = max(mp.mae_r, -r_now)
            if bar_closed:
                mp.bars_held += 1

            # 1) a position must always have a server-side stop
            if not mp.current_stop:
                actions.append(self._ensure_stop(mp, spec))
                continue

            # 2) break-even
            if not mp.moved_to_be and r_now >= e.break_even_at_r:
                be = mp.entry + e.break_even_offset_r * mp.risk * mp.direction.sign
                if self._improves(mp, be):
                    a = self._safe_modify(mp, be, spec, f"break_even: reached {r_now:.2f}R")
                    if a.applied:
                        mp.moved_to_be = True
                    actions.append(a)

            # 3) partial take-profit
            if e.partial_tp_enabled and not mp.partial_taken and r_now >= mp.plan.primary_target.rr:
                vol = spec.normalize_volume(mp.remaining * e.partial_tp_fraction)
                if vol >= spec.volume_min and mp.remaining - vol >= spec.volume_min:
                    try:
                        result = self.broker.close_position(mp.ticket, volume=vol)
                    except BrokerError as exc:
                        result = None
                        log.warning("partial_close_failed", ticket=mp.ticket, error=str(exc))
                    if result is not None and result.ok:
                        mp.remaining -= vol
                        mp.partial_taken = True
                        a = ManagementAction(
                            mp.ticket,
                            "PARTIAL",
                            volume=vol,
                            reason=f"took {vol} lots at {r_now:.2f}R",
                            applied=True,
                        )
                        actions.append(a)
                        if self.on_event:
                            self.on_event(a)

            # 4) structural trailing — behind swing points, never a fixed pip trail
            if e.trail_enabled and r_now >= e.trail_activate_r:
                new_stop = self._structural_trail(mp, snapshot, price, spec)
                if new_stop is not None and self._improves(mp, new_stop):
                    actions.append(
                        self._safe_modify(mp, new_stop, spec, f"trail: {r_now:.2f}R in profit")
                    )

            # 5) time stop — a thesis that has not begun to work is tying up risk budget
            if e.time_stop_bars and mp.bars_held >= e.time_stop_bars and r_now < e.time_stop_min_r:
                actions.append(
                    self._close(
                        mp, ExitReason.TIME_STOP, f"{mp.bars_held} bars held at only {r_now:.2f}R"
                    )
                )
                continue

            # 6) invalidation — the structural premise broke even though the stop held
            if e.invalidation_exit_enabled and snapshot is not None and bar_closed:
                if self._invalidated(mp, snapshot):
                    actions.append(
                        self._close(
                            mp,
                            ExitReason.INVALIDATION,
                            "the structural premise of the setup has broken",
                        )
                    )
                    continue

            # 7) weekend flattening — gold gaps over the weekend
            if e.flat_before_weekend and self._near_weekend(now):
                actions.append(
                    self._close(mp, ExitReason.WEEKEND_FLAT, "flattening before the weekly close")
                )
        return actions

    # -- helpers -----------------------------------------------------------------------

    def _improves(self, mp: ManagedPosition, new_stop: float) -> bool:
        return (
            new_stop > mp.current_stop
            if mp.direction is Direction.LONG
            else new_stop < mp.current_stop
        )

    def _safe_modify(
        self, mp: ManagedPosition, new_stop: float, spec: SymbolSpec, reason: str
    ) -> ManagementAction:
        """Respect the broker's stops_level and freeze_level before sending."""
        try:
            quote = self.broker.quote(mp.plan.symbol or "")
            price = quote.exit_price_for(mp.direction)
        except BrokerError:
            price = mp.entry
        if abs(price - new_stop) < spec.stops_level_price:
            return ManagementAction(
                mp.ticket,
                "TRAIL",
                mp.current_stop,
                new_stop,
                reason=f"{reason} (skipped: inside stops_level)",
            )
        if spec.freeze_level_price and abs(price - mp.current_stop) < spec.freeze_level_price:
            return ManagementAction(
                mp.ticket,
                "TRAIL",
                mp.current_stop,
                new_stop,
                reason=f"{reason} (skipped: inside freeze_level)",
            )
        return self.modify_stop(mp, spec.normalize_price(new_stop), reason)

    def _ensure_stop(self, mp: ManagedPosition, spec: SymbolSpec) -> ManagementAction:
        """A position without a server-side stop is closed if one cannot be attached."""
        try:
            result = self.broker.modify_position(mp.ticket, sl=mp.initial_stop)
        except BrokerError as exc:
            result = None
            log.error("stop_attach_failed", ticket=mp.ticket, error=str(exc))
        if result is not None and result.ok:
            mp.current_stop = mp.initial_stop
            return ManagementAction(
                mp.ticket,
                "ATTACH_STOP",
                None,
                mp.initial_stop,
                reason="restored the server-side stop",
                applied=True,
            )
        log.error(
            "closing_unprotected_position",
            ticket=mp.ticket,
            detail="could not attach a server-side stop; closing rather than running naked",
        )
        return self._close(mp, ExitReason.KILL_SWITCH, "no server-side stop could be attached")

    def _structural_trail(
        self,
        mp: ManagedPosition,
        snapshot: MarketSnapshot | None,
        price: float,
        spec: SymbolSpec,
    ) -> float | None:
        """Trail behind the most recent swing, not a fixed distance."""
        if snapshot is None:
            return None
        st = snapshot.structures.get(mp.plan.setup_timeframe)
        if st is None or not st.swings:
            return None
        from xauusd.domain.enums import SwingKind

        if mp.direction is Direction.LONG:
            lows = [s.price for s in st.swings if s.kind is SwingKind.LOW and s.price < price]
            if not lows:
                return None
            return max(lows) - 0.1 * mp.risk
        highs = [s.price for s in st.swings if s.kind is SwingKind.HIGH and s.price > price]
        if not highs:
            return None
        return min(highs) + 0.1 * mp.risk

    def _invalidated(self, mp: ManagedPosition, snapshot: MarketSnapshot) -> bool:
        st = snapshot.structures.get(mp.plan.setup_timeframe)
        if st is None or st.last_mss is None:
            return False
        return st.last_mss.direction is mp.direction.opposite and st.last_mss.ts > mp.opened_at

    @staticmethod
    def _near_weekend(now: datetime) -> bool:
        return now.weekday() == 4 and now.hour >= 19

    def _close(self, mp: ManagedPosition, reason: ExitReason, detail: str) -> ManagementAction:
        action = ManagementAction(mp.ticket, "CLOSE", reason=f"{reason}: {detail}")
        try:
            result = self.broker.close_position(mp.ticket)
        except BrokerError as exc:
            action.reason += f" (broker error: {exc})"
            return action
        if result.ok:
            action.applied = True
            self.forget(mp.ticket)
            log.info("position_closed", ticket=mp.ticket, reason=str(reason), detail=detail)
        else:
            action.reason += f" (rejected: {result.retcode_text})"
        if self.on_event:
            self.on_event(action)
        return action
