"""The scalp decision pipeline: models to broker, in one place.

Every piece this joins already existed and was tested alone. That is precisely the
danger: this project has produced six components that were complete, correct in
isolation, and connected to nothing — four producer/consumer pairs, a verifier running a
different code path from the thing it verified, and two models geometrically incapable
of firing. Each looked finished from either end.

So this module is deliberately thin and does exactly one thing: run the stages in order,
record what happened at every stage, and refuse on the first hard failure. It contains
no detection logic, no scoring arithmetic and no risk arithmetic. If a number here looks
interesting, it came from somewhere else and belongs to that thing's tests.

    detect      enabled models only
    score       ScalpScorer, against the configured threshold
    economics   cost ratio and net expectancy — the hard filter for small targets
    correlation the budgets that make N positions N bets
    risk        the existing RiskGate, unchanged, with its unchanged caps

The risk stage is the existing one on purpose. A scalp reaching the broker goes through
the same sizing, the same broker cross-check and the same daily/weekly/monthly lockouts
as an A/A+ trade. There is one path to money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from xauusd.config.settings import Settings
from xauusd.core.micro_structure import MicroSnapshot
from xauusd.domain.enums import Classification
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    GateResult,
    MarketSnapshot,
    SymbolSpec,
    TradePlan,
)
from xauusd.engine.continuous import ScanOutcome
from xauusd.monitoring.logging import get_logger
from xauusd.risk.correlation import (
    CorrelationLimits,
    OpenExposure,
    evaluate_correlation,
)
from xauusd.risk.cost_model import CostModel
from xauusd.risk.gate import RiskGate
from xauusd.strategy.scalp.base import ScalpSignal
from xauusd.strategy.scalp.models import default_scalp_registry
from xauusd.strategy.scalp_gates import ScalpEconomics, evaluate_economics
from xauusd.strategy.scalp_score import ScalpScorer

log = get_logger(__name__)


@dataclass(slots=True)
class ScalpEvaluation:
    """One signal's full journey, whether or not it traded."""

    signal: ScalpSignal
    score: float
    score_detail: dict[str, object]
    checks: list[GateResult] = field(default_factory=list)
    approved: bool = False
    rejected_by: str | None = None
    plan: TradePlan | None = None
    volume: float = 0.0
    risk_pct: float = 0.0
    # The sizer's own result, carried rather than recomputed: the execution path reads
    # it directly, and a second sizing would be a second answer free to disagree.
    sizing: object | None = None

    @property
    def summary(self) -> str:
        verdict = "ACCEPTED" if self.approved else f"rejected: {self.rejected_by}"
        return f"{self.signal.model} {self.signal.direction} score {self.score:.0f} — {verdict}"


@dataclass(slots=True)
class ScalpCycle:
    ts: datetime
    evaluations: list[ScalpEvaluation] = field(default_factory=list)
    executable: ScalpEvaluation | None = None
    skipped: str | None = None  # set when the cycle never reached the models

    def as_outcome(self, duration_ms: int) -> ScanOutcome:
        """Fold into the scanner's telemetry, so one counter serves the dashboard."""
        rejections: dict[str, int] = {}
        if self.skipped:
            rejections[self.skipped] = 1
        for e in self.evaluations:
            if e.rejected_by:
                rejections[e.rejected_by] = rejections.get(e.rejected_by, 0) + 1
        return ScanOutcome(
            ts=self.ts,
            duration_ms=duration_ms,
            signals_detected=len(self.evaluations),
            signals_accepted=1 if self.executable else 0,
            rejections=rejections,
        )


class ScalpPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        risk_gate: RiskGate | None = None,
        registry=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings or Settings()
        self.cfg = self.settings.scalp
        self.registry = registry or default_scalp_registry(self.settings)
        self.scorer = ScalpScorer(self.settings.scalp_score)
        self.risk_gate = risk_gate or RiskGate(self.settings)

    # -- the cycle ---------------------------------------------------------------------

    def run(
        self,
        micro: MicroSnapshot,
        snap: MarketSnapshot,
        *,
        account: AccountState,
        spec: SymbolSpec,
        now: datetime,
        open_positions: list[BrokerPosition] | None = None,
        open_risk_pct: float = 0.0,
        trades_today: int = 0,
        exposures: list[OpenExposure] | None = None,
        broker_calc_profit=None,  # type: ignore[no-untyped-def]
    ) -> ScalpCycle:
        cycle = ScalpCycle(ts=now)
        positions = open_positions or []
        exposures = exposures or []

        if not self.cfg.enabled:
            cycle.skipped = "scalp engine disabled"
            return cycle
        enabled = self.registry.enabled(self.cfg.enabled_models)
        if not enabled:
            cycle.skipped = "no scalp models enabled"
            return cycle
        if not micro.usable:
            cycle.skipped = f"micro data unusable: {', '.join(micro.degraded) or 'warming up'}"
            return cycle
        if len(positions) >= self.cfg.max_concurrent:
            cycle.skipped = f"at max concurrent ({self.cfg.max_concurrent})"
            return cycle

        costs = CostModel(
            spec,
            commission_per_lot=self.settings.risk.commission_per_lot,
            slippage_points=self.settings.risk.slippage_points_estimate,
            max_spread_points=self.settings.execution.max_spread_points,
        )
        spread_points = snap.volatility.spread_points or None

        for model in enabled:
            try:
                signals = model.detect(micro, snap) or []
            except Exception as exc:
                log.error("scalp_model_failed", model=model.meta.name, error=str(exc))
                continue
            for signal in signals:
                ev = self._evaluate(
                    signal,
                    snap,
                    micro,
                    costs,
                    spread_points,
                    account,
                    spec,
                    now,
                    positions,
                    open_risk_pct,
                    trades_today,
                    exposures,
                    broker_calc_profit,
                )
                cycle.evaluations.append(ev)
                if ev.approved and cycle.executable is None:
                    cycle.executable = ev

        return cycle

    # -- one signal --------------------------------------------------------------------

    def _evaluate(
        self,
        signal: ScalpSignal,
        snap: MarketSnapshot,
        micro: MicroSnapshot,
        costs: CostModel,
        spread_points: float | None,
        account: AccountState,
        spec: SymbolSpec,
        now: datetime,
        positions: list[BrokerPosition],
        open_risk_pct: float,
        trades_today: int,
        exposures: list[OpenExposure],
        broker_calc_profit,  # type: ignore[no-untyped-def]
    ) -> ScalpEvaluation:
        score = self.scorer.score(signal.factors)
        ev = ScalpEvaluation(signal=signal, score=score.total, score_detail=score.as_dict())

        # --- stage 1: score --------------------------------------------------------
        passed = score.total >= self.cfg.min_score
        ev.checks.append(
            GateResult(
                "scalp_score",
                passed,
                round(score.total, 1),
                self.cfg.min_score,
                detail="weakest: " + ", ".join(score.weakest[:3]) if not passed else "",
            )
        )
        if not passed:
            ev.rejected_by = "scalp_score"
            return ev

        # --- stage 2: is the target worth having at all? ----------------------------
        # Below this the win rate stops being worth buying: a high hit rate on a tiny
        # target is the classic way to lose money slowly.
        if signal.gross_rr < self.cfg.min_gross_rr:
            ev.checks.append(
                GateResult(
                    "scalp_min_gross_rr",
                    False,
                    round(signal.gross_rr, 2),
                    self.cfg.min_gross_rr,
                    detail="target too small to be worth its own costs at any win rate",
                )
            )
            ev.rejected_by = "scalp_min_gross_rr"
            return ev

        # --- stage 3: economics ------------------------------------------------------
        econ = ScalpEconomics(
            cost_model=costs,
            stop_distance=signal.stop_distance,
            gross_rr=signal.gross_rr,
            win_probability=self.cfg.assumed_win_probability,
            spread_points=spread_points,
            max_cost_fraction=self.cfg.max_cost_fraction,
            min_net_expectancy_r=self.cfg.min_net_expectancy_r,
        )
        econ_checks = evaluate_economics(econ)
        ev.checks.extend(econ_checks)
        failed = next((c for c in econ_checks if not c.passed), None)
        if failed:
            ev.rejected_by = failed.name
            return ev

        # --- stage 4: correlation ----------------------------------------------------
        corr = evaluate_correlation(
            direction=signal.direction,
            risk_pct=self.cfg.risk_pct,
            stop_price=signal.stop_loss,
            now=now,
            open_positions=exposures,
            limits=CorrelationLimits(
                max_total_open_risk_pct=self.settings.risk.max_total_open_risk_pct
            ),
            atr=micro.atr_m5,
            model=signal.model,
            liquidity_ref=signal.liquidity_ref,
        )
        ev.checks.extend(corr.checks)
        if not corr.approved:
            ev.rejected_by = corr.blocking[0]
            return ev

        # --- stage 5: the existing risk gate, unchanged ------------------------------
        plan = signal.to_plan(snap.symbol)
        decision = self.risk_gate.evaluate(
            plan=plan,
            classification=Classification.SCALP,
            account=account,
            spec=spec,
            now=now,
            open_positions=positions,
            open_risk_pct=open_risk_pct,
            trades_today=trades_today,
            broker_calc_profit=broker_calc_profit,
        )
        ev.checks.extend(decision.checks)
        ev.plan = plan
        if not decision.approved:
            ev.rejected_by = _first_failure(decision.checks) or "risk"
            return ev

        ev.approved = True
        ev.sizing = decision.sizing
        ev.volume = decision.sizing.lots if decision.sizing else 0.0
        ev.risk_pct = decision.risk_pct_applied
        return ev


def _first_failure(checks) -> str | None:  # type: ignore[no-untyped-def]
    return next((c.name for c in checks if not c.passed), None)
