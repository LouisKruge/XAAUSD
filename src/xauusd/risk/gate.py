"""RiskGate: the ONLY path from a trade plan to a broker order.

Everything the system is forbidden to do is prevented here or is absent from the
interface entirely. The gate re-derives equity, exposure and drawdown from primary
sources at call time; it never trusts a value computed earlier in the decision cycle,
because seconds have passed and the account may have moved.

An invariant violation RAISES and trips the kill switch rather than clamping silently.
A sizing calculation that produces an over-limit number is evidence that something
upstream is wrong, and quietly correcting it would hide the fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from xauusd.config.settings import Settings
from xauusd.domain.enums import Classification, KillSwitchReason
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    GateResult,
    SizingResult,
    SymbolSpec,
    TradePlan,
)
from xauusd.monitoring.logging import get_logger
from xauusd.risk.drawdown import DrawdownGuard
from xauusd.risk.kill_switch import KillSwitch
from xauusd.risk.position_sizing import (
    PositionSizer,
    RiskInvariantViolation,
    SizingInputs,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    sizing: SizingResult | None
    checks: tuple[GateResult, ...]
    reason: str
    risk_pct_applied: float = 0.0

    @property
    def lots(self) -> float:
        return self.sizing.lots if self.sizing and self.sizing.approved else 0.0

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.passed)


class RiskGate:
    def __init__(
        self,
        settings: Settings | None = None,
        drawdown: DrawdownGuard | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.sizer = PositionSizer(self.settings.risk)
        self.drawdown = drawdown or DrawdownGuard(self.settings.risk)
        self.kill_switch = kill_switch or KillSwitch()

    # -- risk budget -------------------------------------------------------------------

    def approved_risk_pct(
        self,
        classification: Classification,
        open_risk_pct: float = 0.0,
        confidence: float | None = None,
    ) -> tuple[float, dict[str, float]]:
        """The MINIMUM of every applicable cap. A+ never means "risk 2% automatically".

        Returns (risk_pct, the caps considered) so the decision journal can show which
        constraint was binding.
        """
        r = self.settings.risk
        class_cap = {
            Classification.A_PLUS: r.risk_pct_a_plus,
            Classification.A: r.risk_pct_a,
            Classification.SCALP: self.settings.scalp.risk_pct,
            Classification.NO_TRADE: 0.0,
        }[classification]

        caps = {
            "class_cap": class_cap,
            "global_cap": r.global_risk_cap_pct,
            "drawdown_budget": self.drawdown.remaining_budget_pct(),
            "exposure_headroom": max(0.0, r.max_total_open_risk_pct - open_risk_pct),
        }
        # Optional confidence scaling: a setup at the bottom of its class band is sized
        # below the cap. Never scales ABOVE the cap.
        if confidence is not None:
            caps["confidence_scaled"] = class_cap * max(0.5, min(1.0, confidence))
        return min(caps.values()), caps

    # -- the gate ----------------------------------------------------------------------

    def evaluate(
        self,
        plan: TradePlan,
        classification: Classification,
        account: AccountState,
        spec: SymbolSpec,
        now: datetime,
        open_positions: list[BrokerPosition] | None = None,
        open_risk_pct: float = 0.0,
        trades_today: int = 0,
        broker_calc_profit: float | None = None,
        broker_calc_margin: float | None = None,
        fx_rate_to_account: float = 1.0,
        confidence: float | None = None,
    ) -> RiskDecision:
        r = self.settings.risk
        checks: list[GateResult] = []
        positions = open_positions or []

        # --- re-read state, do not trust the cycle ------------------------------------
        self.drawdown.update(now, account.equity)

        blocked, ks_reason = self.kill_switch.blocks_entry()
        checks.append(GateResult("risk.kill_switch", not blocked, ks_reason or "clear", "clear"))

        checks.append(
            GateResult(
                "risk.classification",
                classification is not Classification.NO_TRADE,
                str(classification),
                "A or A_PLUS",
            )
        )

        for period in ("DAY", "WEEK", "MONTH"):
            st = self.drawdown.periods.get(period)
            checks.append(
                GateResult(
                    f"risk.{period.lower()}_drawdown",
                    st is None or not st.locked,
                    f"{st.drawdown_pct:.3%}" if st else "unknown",
                    f"< {st.limit_pct:.2%}" if st else "n/a",
                    detail=(st.lock_reason or "") if st else "",
                )
            )

        checks.append(
            GateResult(
                "risk.concurrent_positions",
                len(positions) < r.max_concurrent_positions,
                len(positions),
                r.max_concurrent_positions,
            )
        )
        checks.append(
            GateResult(
                "risk.total_open_risk",
                open_risk_pct < r.max_total_open_risk_pct,
                round(open_risk_pct, 5),
                r.max_total_open_risk_pct,
            )
        )
        checks.append(
            GateResult(
                "risk.trades_per_day",
                trades_today < r.max_trades_per_day,
                trades_today,
                r.max_trades_per_day,
            )
        )

        # Never add to, or hedge, an existing position on the same symbol. The absence
        # of an averaging path in the Broker interface makes this belt-and-braces.
        same_symbol = [p for p in positions if p.symbol == plan.symbol_hint()]
        checks.append(
            GateResult(
                "risk.no_stacking",
                not same_symbol,
                [p.ticket for p in same_symbol] or "none",
                "no existing position",
                detail="never average into or hedge an existing position",
            )
        )

        # The 1:2 floor is the A/A+ engine's, and it is unchanged for A/A+. It is the
        # WRONG question for a trade held minutes: it measures reward against risk and
        # ignores what the trade costs to open and close. The scalp tier answers that
        # question with the cost and net-expectancy gates instead, which are stricter
        # for a setup whose costs eat it, and it carries its own gross-RR floor so a
        # high win rate cannot be bought by shrinking the target to nothing.
        min_rr = self.settings.min_rr_for(classification)
        checks.append(
            GateResult(
                "risk.min_rr",
                plan.rr >= min_rr,
                round(plan.rr, 3),
                min_rr,
                detail=(
                    "scalp floor; net expectancy is the binding economic test"
                    if classification is Classification.SCALP
                    else ""
                ),
            )
        )

        if not all(c.passed for c in checks):
            first = next(c for c in checks if not c.passed)
            return RiskDecision(
                False,
                None,
                tuple(checks),
                f"{first.name}: observed {first.observed!r}, required {first.threshold!r}",
            )

        # --- sizing -------------------------------------------------------------------
        risk_pct, caps = self.approved_risk_pct(classification, open_risk_pct, confidence)
        binding = min(caps, key=lambda k: caps[k])
        checks.append(
            GateResult(
                "risk.budget_available",
                risk_pct > 0,
                round(risk_pct, 6),
                "> 0",
                detail=f"binding constraint: {binding} ({caps[binding]:.4%})",
            )
        )
        if risk_pct <= 0:
            return RiskDecision(
                False,
                None,
                tuple(checks),
                f"no risk budget available; binding constraint is {binding}",
            )

        try:
            sizing = self.sizer.calculate(
                SizingInputs(
                    equity=account.equity,
                    risk_pct=risk_pct,
                    entry=plan.entry,
                    stop_loss=plan.stop_loss,
                    direction=plan.direction,
                    spec=spec,
                    account_currency=account.currency,
                    fx_rate_to_account=fx_rate_to_account,
                    free_margin=account.free_margin,
                    commission_per_lot=spec.commission_per_lot or r.commission_per_lot,
                ),
                broker_calc_profit=broker_calc_profit,
                broker_calc_margin=broker_calc_margin,
            )
        except RiskInvariantViolation as exc:
            # This is a bug, not a market condition. Halt.
            self.kill_switch.trip(
                KillSwitchReason.RISK_INVARIANT,
                str(exc),
                {"strategy": plan.strategy, "classification": str(classification)},
                now,
            )
            checks.append(GateResult("risk.invariant", False, str(exc), "no violation"))
            return RiskDecision(False, None, tuple(checks), f"risk invariant violated: {exc}")

        checks.append(
            GateResult(
                "risk.sizing",
                sizing.approved,
                sizing.lots,
                f"> {spec.volume_min}",
                detail=sizing.reason,
            )
        )
        if not sizing.approved:
            return RiskDecision(False, sizing, tuple(checks), sizing.reason, risk_pct)

        # --- final invariant, belt and braces ----------------------------------------
        realised_pct = sizing.risk_money / account.equity if account.equity > 0 else 1.0
        ceiling = risk_pct * (1 + r.risk_overshoot_tolerance)
        ok = realised_pct <= ceiling
        checks.append(
            GateResult(
                "risk.final_invariant",
                ok,
                round(realised_pct, 6),
                round(ceiling, 6),
                detail="stop-loss risk as a fraction of live equity",
            )
        )
        if not ok:
            self.kill_switch.trip(
                KillSwitchReason.RISK_INVARIANT,
                f"final sizing check failed: {realised_pct:.4%} > {ceiling:.4%}",
                now=now,
            )
            return RiskDecision(
                False, sizing, tuple(checks), "final risk invariant failed", risk_pct
            )

        log.info(
            "risk_approved",
            strategy=plan.strategy,
            classification=str(classification),
            lots=sizing.lots,
            risk_pct=round(realised_pct, 5),
            risk_money=round(sizing.risk_money, 2),
            binding_constraint=binding,
            rr=round(plan.rr, 2),
        )
        return RiskDecision(True, sizing, tuple(checks), "approved", risk_pct)
