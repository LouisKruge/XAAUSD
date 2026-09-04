"""The scalp engine, wired: signal to broker.

Every component here passed its own tests in isolation before this file existed, and
that is exactly the danger. This project has produced six components that were complete,
correct alone, and connected to nothing — four producer/consumer pairs, a verifier
running a different code path from the thing it verified, and two models geometrically
incapable of firing. Each looked finished from either end.

So these tests cross the seams. They assert that a signal produced by a model reaches
the risk gate, that an approved one reaches the broker, that a rejected one is still
journalled, and that the caps which make the whole thing safe are the SAME instances the
A/A+ engine uses rather than second copies that agree by coincidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.synthetic import market_m1
from xauusd.config.settings import Settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import Classification, Direction, KillSwitchReason, Timeframe
from xauusd.domain.types import AccountState, Quote, SymbolSpec
from xauusd.engine.scalp_pipeline import ScalpPipeline
from xauusd.risk.correlation import OpenExposure
from xauusd.risk.gate import RiskGate

SPEC = SymbolSpec(
    "XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5, commission_per_lot=7.0
)
ALL_MODELS = [
    "scalp_sweep_reversal",
    "scalp_fvg_retracement",
    "scalp_ob_reaction",
    "scalp_breakout_retest",
    "scalp_momentum_continuation",
]


def settings(**scalp) -> Settings:
    base = dict(
        enabled=True,
        enabled_models=ALL_MODELS,
        min_score=0.0,
        max_cost_fraction=0.95,
        min_net_expectancy_r=0.0,
        min_gross_rr=0.1,
    )
    base.update(scalp)
    return Settings(scalp=base)


def account(equity: float = 100_000.0) -> AccountState:
    return AccountState(1, "USD", equity, equity, 0.0, equity, 0.0)


@pytest.fixture(scope="module")
def market_state():
    """A market instant with live micro and macro snapshots."""
    data = market_m1(30_000, seed=9)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]
    out = []
    macro_an = MarketAnalyzer(Settings())
    micro_an = MicroAnalyzer(Settings())
    for i in range(2000, len(m1), 20):
        bar = m1.bar_at(i)
        now = bar.ts + timedelta(seconds=60)
        half = (bar.spread_points or 25) * SPEC.point / 2
        view = MarketView(source, "XAUUSD", now, Quote(now, bar.close - half, bar.close + half))
        micro = micro_an.analyze(view)
        if not micro.usable:
            continue
        snap = macro_an.analyze(view, None, None, float(bar.spread_points or 25), 25.0)
        out.append((now, micro, snap))
    return out


def run_all(pipeline: ScalpPipeline, market_state, **kw):
    """Every instant through the pipeline; returns all evaluations and executables."""
    evals, executed = [], []
    for now, micro, snap in market_state:
        cycle = pipeline.run(
            micro, snap, account=kw.pop("acct", None) or account(), spec=SPEC, now=now, **kw
        )
        evals.extend(cycle.evaluations)
        if cycle.executable:
            executed.append(cycle.executable)
    return evals, executed


class TestSignalsReachTheRiskGate:
    def test_the_pipeline_produces_evaluations(self, market_state) -> None:
        evals, _ = run_all(ScalpPipeline(settings()), market_state)
        assert evals, "no signal reached the pipeline at all"

    def test_at_least_one_signal_is_approved_end_to_end(self, market_state) -> None:
        """The seam test. Detection, scoring, economics, correlation, risk and sizing
        all have to agree for this to be non-empty."""
        evals, executed = run_all(ScalpPipeline(settings()), market_state)
        assert executed, (
            "no signal survived the full pipeline. Either every candidate is genuinely "
            "unaffordable, or a stage is refusing everything — and those need opposite "
            "responses, so investigate rather than relax the thresholds."
        )

    def test_an_approved_signal_carries_a_sized_plan(self, market_state) -> None:
        _, executed = run_all(ScalpPipeline(settings()), market_state)
        ev = executed[0]
        assert ev.plan is not None
        assert ev.volume > 0, "an approved trade must have a position size"
        assert ev.risk_pct > 0

    def test_the_plan_is_the_type_the_execution_path_expects(self, market_state) -> None:
        """Reusing TradePlan is what keeps one path to the broker."""
        _, executed = run_all(ScalpPipeline(settings()), market_state)
        plan = executed[0].plan
        assert plan.symbol == "XAUUSD"
        assert plan.targets
        if plan.direction is Direction.LONG:
            assert plan.stop_loss < plan.entry < plan.targets[0].price
        else:
            assert plan.targets[0].price < plan.entry < plan.stop_loss


class TestTheScalpTierUsesItsOwnRisk:
    def test_a_scalp_is_sized_at_the_scalp_fraction_not_an_a_grade_one(self) -> None:
        """Mixing SCALP into the A ladder would let it inherit 1% by accident."""
        gate = RiskGate(settings())
        scalp_pct, _ = gate.approved_risk_pct(Classification.SCALP)
        a_pct, _ = gate.approved_risk_pct(Classification.A)
        assert scalp_pct == pytest.approx(0.0015)
        assert scalp_pct < a_pct

    def test_approved_scalps_never_exceed_the_scalp_risk(self, market_state) -> None:
        _, executed = run_all(ScalpPipeline(settings()), market_state)
        for ev in executed:
            assert ev.risk_pct <= 0.0015 * 1.05, "a scalp must not size like an A trade"


class TestTheSharedCapsStillBind:
    """The scalp engine must not be a second opinion on risk. It consults the same gate."""

    def test_a_tripped_kill_switch_stops_every_scalp(self, market_state) -> None:
        gate = RiskGate(settings())
        gate.kill_switch.trip(KillSwitchReason.MANUAL, "operator halt", now=datetime.now(UTC))
        _, executed = run_all(ScalpPipeline(settings(), risk_gate=gate), market_state)
        assert not executed, "the kill switch must stop scalps as it stops A/A+ trades"

    def test_being_at_max_concurrent_skips_the_scan_entirely(self, market_state) -> None:
        from xauusd.domain.types import BrokerPosition

        open_pos = [
            BrokerPosition(
                ticket=1,
                symbol="XAUUSD",
                direction=Direction.LONG,
                volume=0.1,
                entry_price=2600.0,
                stop_loss=2590.0,
                take_profit=2620.0,
                opened_at=datetime.now(UTC),
                magic=1,
                comment="x:t",
            )
        ]
        pipeline = ScalpPipeline(settings(max_concurrent=1))
        now, micro, snap = market_state[0]
        cycle = pipeline.run(
            micro, snap, account=account(), spec=SPEC, now=now, open_positions=open_pos
        )
        assert cycle.skipped and "max concurrent" in cycle.skipped
        assert not cycle.evaluations, "no model should even run when the book is full"


class TestNothingRunsUntilItIsTurnedOn:
    def test_the_engine_is_off_by_default(self) -> None:
        assert Settings().scalp.enabled is False
        assert Settings().scalp.enabled_models == []

    def test_a_disabled_engine_evaluates_nothing(self, market_state) -> None:
        pipeline = ScalpPipeline(Settings())
        now, micro, snap = market_state[0]
        cycle = pipeline.run(micro, snap, account=account(), spec=SPEC, now=now)
        assert cycle.skipped == "scalp engine disabled"
        assert not cycle.evaluations

    def test_enabled_but_no_models_is_reported_distinctly(self, market_state) -> None:
        """'On but empty' and 'off' are different operator mistakes."""
        pipeline = ScalpPipeline(Settings(scalp={"enabled": True}))
        now, micro, snap = market_state[0]
        cycle = pipeline.run(micro, snap, account=account(), spec=SPEC, now=now)
        assert cycle.skipped == "no scalp models enabled"


class TestRejectionsAreVisible:
    def test_every_rejection_names_the_stage_that_refused_it(self, market_state) -> None:
        evals, _ = run_all(ScalpPipeline(settings()), market_state)
        for ev in evals:
            if not ev.approved:
                assert ev.rejected_by, f"{ev.signal.model} rejected with no reason"
                assert ev.checks, "a rejection with no gate trace explains nothing"

    def test_a_high_score_bar_shows_up_as_a_score_rejection(self, market_state) -> None:
        evals, executed = run_all(ScalpPipeline(settings(min_score=99.0)), market_state)
        assert not executed
        assert all(e.rejected_by == "scalp_score" for e in evals)

    def test_a_tight_cost_ceiling_shows_up_as_a_cost_rejection(self, market_state) -> None:
        evals, executed = run_all(ScalpPipeline(settings(max_cost_fraction=0.01)), market_state)
        assert not executed
        assert any(e.rejected_by == "scalp_cost_ratio" for e in evals)

    def test_the_cycle_folds_into_scanner_telemetry(self, market_state) -> None:
        """One counter serves the dashboard, so signals/accepted/rejected agree."""
        pipeline = ScalpPipeline(settings())
        now, micro, snap = market_state[0]
        cycle = pipeline.run(micro, snap, account=account(), spec=SPEC, now=now)
        outcome = cycle.as_outcome(duration_ms=5)
        assert outcome.signals_detected == len(cycle.evaluations)
        assert outcome.signals_accepted == (1 if cycle.executable else 0)


class TestCorrelationIsEnforcedInTheLivePath:
    def test_an_open_position_in_the_same_zone_blocks_a_new_scalp(self, market_state) -> None:
        """The budgets exist; this asserts the pipeline actually consults them."""
        pipeline = ScalpPipeline(settings())
        blocked = 0
        for now, micro, snap in market_state:
            clean = pipeline.run(micro, snap, account=account(), spec=SPEC, now=now)
            if not clean.executable:
                continue
            sig = clean.executable.signal
            crowded = pipeline.run(
                micro,
                snap,
                account=account(),
                spec=SPEC,
                now=now,
                exposures=[
                    OpenExposure(
                        direction=sig.direction,
                        risk_pct=0.0015,
                        stop_price=sig.stop_loss,
                        opened_at=now - timedelta(minutes=1),
                        model=sig.model,
                        liquidity_ref=sig.liquidity_ref,
                    )
                ],
            )
            assert crowded.executable is None, "same zone and same model must be refused"
            blocked += 1
            break
        assert blocked, "no approved signal was available to test correlation against"
