"""The decision pipeline: MarketView in, one auditable Decision out.

Runs the ten stages from docs/architecture/00-overview.md in order. The same object
serves live and backtest, because everything it depends on is injected: the broker, the
clock, the probability model. That is what makes backtest and live the same code path
rather than two implementations that drift apart over months.

A Decision is produced on EVERY evaluation, including the thousands that end in
NO_TRADE, each carrying its full feature vector and gate trace. That record is
simultaneously the explainability surface, the rejection ledger, and the training set
for the probability model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from xauusd.config.settings import Settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.core.sessions import SessionEngine
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Classification, Direction, ValidationStatus
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    Decision,
    GateResult,
    MacroState,
    MarketSnapshot,
    NewsState,
    SymbolSpec,
    TradePlan,
)
from xauusd.execution.broker import BrokerHealth
from xauusd.intelligence.news import NewsEngine
from xauusd.monitoring.logging import get_logger
from xauusd.risk.gate import RiskDecision, RiskGate
from xauusd.strategy.base import StrategyRegistry, default_registry
from xauusd.strategy.classifier import Classifier
from xauusd.strategy.features import FeatureVector, extract
from xauusd.strategy.gates import GateContext, run_gates
from xauusd.strategy.scoring import ScoringEngine, reasons_for_and_against

log = get_logger(__name__)


class ProbabilityModel(Protocol):
    """Optional. When absent the system degrades to score-only in A-only mode."""

    model_id: str

    def predict(self, features: FeatureVector) -> float | None: ...

    def is_healthy(self) -> bool: ...


@dataclass(slots=True)
class EngineState:
    """Live account and risk facts, refreshed by the caller before each cycle."""

    account: AccountState | None = None
    spec: SymbolSpec | None = None
    health: BrokerHealth | None = None
    open_positions: list[BrokerPosition] = field(default_factory=list)
    open_risk_pct: float = 0.0
    trades_today: int = 0
    existing_tags: set[str] = field(default_factory=set)
    symbol_resolved: bool = True
    spec_unchanged: bool = True
    strategy_status: dict[str, ValidationStatus] = field(default_factory=dict)
    fx_rate_to_account: float = 1.0

    # The broker's own answer to "what does one lot lose between these two prices?".
    # PositionSizer cross-checks its arithmetic against this and refuses to trade when
    # they disagree by more than the configured tolerance — which is the only thing that
    # catches a symbol specification whose tick value does not match its contract size
    # and tick size. Left as None the cross-check silently never runs, which is how it
    # sat for the whole of development.
    calc_profit: Callable[[Direction, float, float], float | None] | None = None


@dataclass(slots=True)
class CycleResult:
    """Everything one evaluation produced. Several decisions when several candidates."""

    ts: datetime
    snapshot: MarketSnapshot
    decisions: list[Decision]
    latency_ms: int
    executable: Decision | None = None

    @property
    def traded(self) -> bool:
        return self.executable is not None


class DecisionPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        registry: StrategyRegistry | None = None,
        analyzer: MarketAnalyzer | None = None,
        risk_gate: RiskGate | None = None,
        model: ProbabilityModel | None = None,
        git_sha: str = "",
    ) -> None:
        self.settings = settings or Settings()
        self.registry = registry or default_registry()
        self.analyzer = analyzer or MarketAnalyzer(self.settings)
        self.scoring = ScoringEngine(
            self.settings.scoring, self.settings.thresholds, self.settings.news
        )
        self.classifier = Classifier(self.settings)
        self.risk_gate = risk_gate or RiskGate(self.settings)
        self.news_engine = NewsEngine(self.settings.news)
        self.sessions = SessionEngine(self.settings.session)
        self.model = model
        self.config_hash = self.settings.config_hash()
        self.git_sha = git_sha

    # -- main entry point --------------------------------------------------------------

    def run(
        self,
        view: MarketView,
        state: EngineState,
        macro: MacroState | None = None,
        news: NewsState | None = None,
        spread_points: float = 0.0,
        spread_median: float = 25.0,
    ) -> CycleResult:
        t0 = time.perf_counter()

        # Stages 1-3: analysis
        snap = self.analyzer.analyze(view, macro, news, spread_points, spread_median)

        # Stage 4: candidate generation
        plans = self._detect(view, snap)

        decisions: list[Decision] = []
        executable: Decision | None = None

        if not plans:
            decisions.append(self._no_candidate_decision(view, snap, state))
        else:
            for plan in plans:
                decision = self._evaluate_plan(view, snap, plan, state)
                decisions.append(decision)
                if decision.is_trade and executable is None:
                    executable = decision

        latency = int((time.perf_counter() - t0) * 1000)
        decisions = [replace(d, latency_ms=latency) for d in decisions]
        executable = next((d for d in decisions if d.is_trade), None)
        return CycleResult(view.now, snap, decisions, latency, executable)

    # -- stages ------------------------------------------------------------------------

    @staticmethod
    def _broker_loss_for_one_lot(state: EngineState, plan: TradePlan) -> float | None:
        """What the broker says one lot loses moving from entry to stop.

        Returns None when unavailable — no broker, or the call failed — because the
        cross-check is corroboration, not a precondition. A broker that cannot answer
        must not stop the engine evaluating; PositionSizer already refuses to trade when
        the answer it gets DISAGREES, which is the case that matters.
        """
        if state.calc_profit is None:
            return None
        try:
            value = state.calc_profit(plan.direction, plan.entry, plan.stop_loss)
            return float(value) if value is not None else None
        except Exception:
            return None

    def _detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]:
        plans: list[TradePlan] = []
        for strategy in self.registry.enabled(self.settings):
            allowed, why = StrategyRegistry.is_allowed_here(strategy, snap)
            if not allowed:
                log.debug("strategy_skipped", strategy=strategy.meta.name, reason=why)
                continue
            try:
                found = strategy.detect(view, snap) or []
            except Exception as exc:
                log.error(
                    "strategy_failed",
                    strategy=strategy.meta.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            for p in found:
                plans.append(p if p.symbol else replace(p, symbol=snap.symbol))
        return plans

    def _evaluate_plan(
        self, view: MarketView, snap: MarketSnapshot, plan: TradePlan, state: EngineState
    ) -> Decision:
        # Stage 6: features and score
        features = extract(snap, plan)
        news_contribution = self.news_engine.score_contribution(snap.news, plan.direction)
        breakdown = self.scoring.score(features, snap, news_contribution)

        # Stage 7: calibrated probability (optional)
        probability: float | None = None
        model_id: str | None = None
        model_health = "UNAVAILABLE"
        if self.model is not None:
            try:
                probability = self.model.predict(features)
                model_id = self.model.model_id
                model_health = "HEALTHY" if self.model.is_healthy() else "DEGRADED"
            except Exception as exc:
                log.error("model_predict_failed", error=str(exc))
                model_health = "ERROR"

        # Stages 0-5: hard gates
        status = state.strategy_status.get(plan.strategy, ValidationStatus.DEV)
        ctx = self._gate_context(snap, plan, state, view, status)
        gate_results = run_gates(ctx)
        gates_passed = all(g.passed for g in gate_results)

        # Stage 8: classification
        cls = self.classifier.classify(
            breakdown=breakdown,
            probability=probability,
            features=features,
            snap=snap,
            plan=plan,
            gates_passed=gates_passed,
            strategy_status=status,
            model_healthy=model_health in ("HEALTHY", "UNAVAILABLE"),
        )
        all_gates = list(gate_results) + list(cls.checks)

        # Stage 9: risk and sizing
        risk: RiskDecision | None = None
        classification = cls.classification
        if classification is not Classification.NO_TRADE and state.account and state.spec:
            risk = self.risk_gate.evaluate(
                plan=plan,
                classification=classification,
                account=state.account,
                spec=state.spec,
                now=snap.ts,
                open_positions=state.open_positions,
                open_risk_pct=state.open_risk_pct,
                trades_today=state.trades_today,
                fx_rate_to_account=state.fx_rate_to_account,
                confidence=probability,
                broker_calc_profit=self._broker_loss_for_one_lot(state, plan),
            )
            all_gates.extend(risk.checks)
            if not risk.approved:
                classification = Classification.NO_TRADE
        elif classification is not Classification.NO_TRADE:
            all_gates.append(
                GateResult(
                    "risk.state_available",
                    False,
                    "no account or spec",
                    "both present",
                    detail="cannot size without live account state",
                )
            )
            classification = Classification.NO_TRADE

        for_, against = reasons_for_and_against(features, breakdown, snap, plan)
        if cls.classification is Classification.NO_TRADE:
            against.insert(0, cls.reason)

        return Decision(
            ts=snap.ts,
            symbol=snap.symbol,
            classification=classification,
            mode=str(self.settings.mode),
            plan=plan,
            score=breakdown.total,
            breakdown=breakdown,
            probability=probability,
            model_id=model_id,
            model_health=model_health,
            features=features.as_dict(),
            gates=tuple(all_gates),
            reasons_for=tuple(for_),
            reasons_against=tuple(against),
            sizing=risk.sizing if risk else None,
            config_hash=self.config_hash,
            git_sha=self.git_sha,
        )

    def _no_candidate_decision(
        self, view: MarketView, snap: MarketSnapshot, state: EngineState
    ) -> Decision:
        """Even a cycle with no candidate is journalled, with WHY there was none.

        Without this the rejection ledger cannot distinguish "the market offered
        nothing" from "a filter is broken and silently rejecting everything", which is
        the single most useful thing to know during paper trading.
        """
        ctx = self._gate_context(snap, None, state, view, ValidationStatus.DEV)
        gates = run_gates(ctx)
        environment_blocks = [g for g in gates if not g.passed and g.name != "has_candidate"]
        against = (
            [f"{g.name}: {g.observed}" for g in environment_blocks]
            if environment_blocks
            else ["environment is tradable but no strategy found a valid setup"]
        )
        return Decision(
            ts=snap.ts,
            symbol=snap.symbol,
            classification=Classification.NO_TRADE,
            mode=str(self.settings.mode),
            gates=tuple(gates),
            reasons_against=tuple(against),
            features={
                "regime": str(snap.regime),
                "session": str(snap.session.session),
                "htf_bias": str(snap.htf_bias),
                "news_risk": str(snap.news.risk),
                "spread_points": snap.volatility.spread_points,
                "sweeps": len(snap.sweeps),
                "fvgs": len(snap.fvgs),
                "order_blocks": len(snap.order_blocks),
            },
            config_hash=self.config_hash,
            git_sha=self.git_sha,
        )

    def _gate_context(
        self,
        snap: MarketSnapshot,
        plan: TradePlan | None,
        state: EngineState,
        view: MarketView,
        status: ValidationStatus,
    ) -> GateContext:
        ks_active, ks_reason = self.risk_gate.kill_switch.blocks_entry()
        dd = self.risk_gate.drawdown
        day = dd.periods.get("DAY")
        week = dd.periods.get("WEEK")
        month = dd.periods.get("MONTH")
        tag = client_tag(plan) if plan else ""
        return GateContext(
            settings=self.settings,
            snapshot=snap,
            plan=plan,
            spec=state.spec,
            account=state.account,
            health=state.health,
            kill_switch_active=ks_active,
            kill_switch_reason=ks_reason,
            quote_age_seconds=view.quote_age_seconds() if view.quote else 0.0,
            bar_age_seconds=view.bar_age_seconds(),
            open_positions=len(state.open_positions),
            open_risk_pct=state.open_risk_pct,
            trades_today=state.trades_today,
            daily_drawdown_pct=day.drawdown_pct if day else 0.0,
            weekly_drawdown_pct=week.drawdown_pct if week else 0.0,
            monthly_drawdown_pct=month.drawdown_pct if month else 0.0,
            daily_locked=bool(day and day.locked),
            weekly_locked=bool(week and week.locked),
            monthly_locked=bool(month and month.locked),
            duplicate_tag=tag in state.existing_tags,
            strategy_status=status,
            symbol_resolved=state.symbol_resolved,
            spec_unchanged=state.spec_unchanged,
            session_engine=self.sessions,
        )


def client_tag(plan: TradePlan) -> str:
    """Deterministic idempotency key, computed BEFORE any network call.

    Depends only on the plan, so the same setup evaluated twice yields the same tag and
    a duplicate can be detected against the broker's own comment field rather than
    against our memory of what we sent.
    """
    import hashlib

    payload = (
        f"{plan.strategy}|{plan.strategy_version}|{plan.direction}|"
        f"{plan.setup_timeframe}|{int(plan.ts.timestamp())}|{round(plan.entry, 4)}"
    )
    return hashlib.blake2s(payload.encode(), digest_size=6).hexdigest()
