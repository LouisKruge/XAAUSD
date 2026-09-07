"""The intraday decision pipeline: Expansion+Pullback setup to broker, in one place.

The counterpart to `scalp_pipeline`, and deliberately built the same way for the same
reason: this project has produced twelve components that were complete, correct in
isolation, and connected to nothing. `ExpansionPullbackEngine` was the twelfth — fully
tested, deciding nothing, because no pipeline consulted it. A strategy that is not wired
is not a strategy, it is a document.

    validated   the model may reach real money at all
    setup       ExpansionPullbackEngine's ordered sequence (§20-§24)
    budget      the per-engine aggregate and daily drawdown (§15, §16)
    risk        the existing RiskGate, unchanged, with its unchanged caps

The risk stage is the existing one on purpose, and so is the sizing and the broker
cross-check. An intraday trade reaching the broker goes through the same choke point as
a scalp and as an A/A+ trade. There is one path to money.

What is NOT here: any detection logic. The sequence lives in the strategy module and is
tested there. This module runs stages in order, records what happened at every one, and
refuses on the first hard failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from xauusd.config.settings import Settings
from xauusd.domain.enums import Classification, Direction, Timeframe, ValidationStatus
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    GateResult,
    MarketSnapshot,
    SymbolSpec,
    TargetLevel,
    TradePlan,
)
from xauusd.monitoring.logging import get_logger
from xauusd.risk.engine_budget import INTRADAY, EngineBudget
from xauusd.risk.gate import RiskGate
from xauusd.strategy.intraday.expansion_pullback import (
    SETUP_TF,
    ExpansionPullbackEngine,
    IntradayEvaluation,
)

log = get_logger(__name__)


@dataclass(slots=True)
class IntradayCycle:
    """One pass, whether or not it produced a trade.

    A cycle that reached 'pullback' and stopped is worth seeing in the journal: the
    difference between "no setup" and "a setup that failed its last check" is the whole
    reason the rejection ledger exists.
    """

    ts: datetime
    evaluation: IntradayEvaluation | None = None
    checks: list[GateResult] = field(default_factory=list)
    approved: bool = False
    rejected_by: str | None = None
    plan: TradePlan | None = None
    volume: float = 0.0
    risk_pct: float = 0.0
    sizing: object | None = None
    skipped: str | None = None

    @property
    def reached(self) -> str:
        return self.evaluation.reached if self.evaluation else "not evaluated"

    @property
    def summary(self) -> str:
        if self.skipped:
            return f"intraday skipped: {self.skipped}"
        verdict = "ACCEPTED" if self.approved else f"rejected: {self.rejected_by}"
        return f"intraday reached {self.reached} — {verdict}"


class IntradayPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        risk_gate: RiskGate | None = None,
        engine: ExpansionPullbackEngine | None = None,
        budget: EngineBudget | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.cfg = self.settings.intraday
        # The engine is held rather than constructed per cycle: its `setup` IS the
        # memory that makes the sequence ordered in time (§22-§24). A fresh engine each
        # cycle would re-derive the expansion and the pullback from one snapshot, which
        # accepts them in either order and is a much weaker claim.
        self.engine = engine or ExpansionPullbackEngine(self.settings)
        self.risk_gate = risk_gate or RiskGate(self.settings)
        self.budget = budget or EngineBudget(self.settings)

    def run(
        self,
        snap: MarketSnapshot,
        *,
        account: AccountState,
        spec: SymbolSpec,
        now: datetime,
        open_positions: list[BrokerPosition] | None = None,
        open_risk_pct: float = 0.0,
        trades_today: int = 0,
        strategy_status: dict[str, ValidationStatus] | None = None,
        calc_profit: Callable[[Direction, float, float], float | None] | None = None,
    ) -> IntradayCycle:
        cycle = IntradayCycle(ts=now)
        positions = open_positions or []
        status = strategy_status or {}

        if not self.cfg.enabled:
            cycle.skipped = "intraday engine disabled"
            return cycle

        # --- stage 0: may this strategy reach real money at all? ----------------------
        # Runs FIRST and unconditionally, for the reason the scalp path had to learn:
        # a check that only runs after the others pass is a check the failing case has
        # never exercised. The intraday engine ships DEV, which is what this refuses.
        st = status.get(self.engine.name, ValidationStatus.DEV)
        live_ok = st.live_eligible or not self.settings.mode.is_real_money
        cycle.checks.append(
            GateResult(
                "intraday_strategy_validated",
                live_ok,
                str(st),
                "OOS_PASSED or better for live trading",
                detail=(
                    ""
                    if live_ok
                    else f"{self.engine.name} has not passed out-of-sample validation; "
                    "live routing refused"
                ),
            )
        )
        if not live_ok:
            cycle.rejected_by = "intraday_strategy_validated"
            return cycle

        # --- stage 1: the ordered sequence -------------------------------------------
        # Always run, even when the engine is already at its position limit, because the
        # sequence is stateful: skipping evaluation would leave the setup memory frozen
        # at whatever it held when the position opened, and the next free cycle would
        # act on a stale expansion. It costs one evaluation and keeps the memory honest.
        ev = self.engine.evaluate(snap, now)
        cycle.evaluation = ev
        cycle.checks.append(
            GateResult(
                "intraday_setup",
                ev.is_entry,
                ev.reached,
                "entry",
                detail="; ".join(ev.reasons),
            )
        )
        if not ev.is_entry or ev.stop_loss is None or ev.target is None or ev.entry is None:
            cycle.rejected_by = "intraday_setup"
            return cycle

        assert ev.direction is not None  # an entry always carries a direction
        plan = self._plan(snap, ev, now)
        cycle.plan = plan

        # --- stage 2: the per-engine budget (§15, §16) --------------------------------
        verdict = self.budget.may_add(INTRADAY, self.cfg.risk_pct, positions, account.equity, spec)
        cycle.checks.append(
            GateResult(
                "intraday_engine_budget",
                verdict.allowed,
                f"{verdict.would_be_pct:.2%}",
                f"<= {verdict.limit_pct:.2%}",
                detail=verdict.reason,
            )
        )
        if not verdict.allowed:
            cycle.rejected_by = "intraday_engine_budget"
            return cycle

        # --- stage 3: the existing risk gate, unchanged -------------------------------
        decision = self.risk_gate.evaluate(
            plan=plan,
            classification=Classification.INTRADAY,
            account=account,
            spec=spec,
            now=now,
            open_positions=positions,
            open_risk_pct=open_risk_pct,
            trades_today=trades_today,
            broker_calc_profit=self._broker_loss_for_one_lot(calc_profit, plan),
            engine=INTRADAY,
        )
        cycle.checks.extend(decision.checks)
        if not decision.approved:
            cycle.rejected_by = next((c.name for c in decision.checks if not c.passed), "risk")
            return cycle

        cycle.approved = True
        cycle.sizing = decision.sizing
        cycle.volume = decision.sizing.lots if decision.sizing else 0.0
        cycle.risk_pct = decision.risk_pct_applied
        return cycle

    def consume(self) -> None:
        """§28: mark the setup traded, so it can never produce a second entry.

        Called by whatever actually opened the position, never by `run`. An entry the
        broker refused must leave the setup available — consuming on the DECISION rather
        than on the FILL would silently retire setups that never traded.
        """
        self.engine.consume()

    # -- helpers ---------------------------------------------------------------------

    def _plan(self, snap: MarketSnapshot, ev: IntradayEvaluation, now: datetime) -> TradePlan:
        """The evaluation as the plan type risk and execution already understand."""
        assert ev.entry is not None and ev.stop_loss is not None and ev.target is not None
        risk = abs(ev.entry - ev.stop_loss)
        rr = abs(ev.target - ev.entry) / risk if risk > 0 else 0.0
        setup = self.engine.setup
        return TradePlan(
            strategy=self.engine.name,
            strategy_version=self.engine.version,
            direction=ev.direction,  # type: ignore[arg-type]
            entry=ev.entry,
            stop_loss=ev.stop_loss,
            targets=(
                TargetLevel(
                    price=ev.target,
                    rr=rr,
                    # What ACTUALLY produced this price, not what §26 hopes produced it.
                    rationale=ev.target_source or "unrecorded",
                ),
            ),
            ts=now,
            setup_timeframe=SETUP_TF,
            symbol=snap.symbol,
            invalidation=(
                f"{SETUP_TF} structure turning against the trade, or price closing "
                "beyond the pullback zone"
            ),
            evidence={
                "engine": INTRADAY,
                "reached": ev.reached,
                "reasons": list(ev.reasons),
                "target_source": ev.target_source,
                "expansion_at": setup.expansion_at.isoformat() if setup else None,
                "expansion_price": setup.expansion_price if setup else None,
                "h4_bias": str(snap.bias(Timeframe.H4)),
                "h1_bias": str(snap.bias(Timeframe.H1)),
            },
        )

    def _broker_loss_for_one_lot(
        self,
        calc_profit: Callable[[Direction, float, float], float | None] | None,
        plan: TradePlan,
    ) -> float | None:
        """The broker's own loss for one lot on THIS plan's entry and stop.

        The same cross-check the A/A+ and scalp paths have. Wired from the start here
        rather than left as a literal `None`, which is how the scalp path spent months
        sizing real money on arithmetic the broker had never confirmed (BUG-002).
        """
        if calc_profit is None:
            return None
        try:
            value = calc_profit(plan.direction, plan.entry, plan.stop_loss)
        except Exception as exc:
            log.error(
                "broker_calc_profit_failed",
                strategy=plan.strategy,
                direction=str(plan.direction),
                error=f"{type(exc).__name__}: {exc}",
                real_money=self.settings.mode.is_real_money,
            )
            if self.settings.mode.is_real_money:
                from xauusd.engine.pipeline import BrokerCrossCheckUnavailable

                raise BrokerCrossCheckUnavailable(
                    f"the broker could not price one lot for {plan.strategy}: "
                    f"{type(exc).__name__}: {exc}. Refusing to size a real-money "
                    f"position on arithmetic the broker cannot confirm."
                ) from exc
            return None
        return float(value) if value is not None else None
