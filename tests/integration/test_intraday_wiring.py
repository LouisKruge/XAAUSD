"""The intraday engine, wired: setup to broker.

`ExpansionPullbackEngine` was the twelfth component in this project that was complete,
tested in isolation, and connected to nothing. It decided nothing because no pipeline
consulted it, and it would have looked finished from either end forever.

So these tests cross the seams, and the two that matter most are structural rather than
behavioural:

  * the LIVE orchestrator and the BACKTESTER both run it. An engine wired into one and
    not the other is the asymmetry that made scalps tradeable-but-unvalidatable
    (BUG-002), and it is worse in this direction: a strategy that can reach real money
    but cannot be measured has skipped the deployment gate entirely.
  * §28 survives execution. `consume()` must be called on a FILL, not on a decision,
    or an order the broker refused silently retires the setup it was for.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.synthetic import market_m1
from xauusd.config.settings import Settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import (
    Bias,
    Classification,
    Direction,
    StructureKind,
    Timeframe,
    ValidationStatus,
)
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    Quote,
    StructureEvent,
    SymbolSpec,
    TimeframeStructure,
)
from xauusd.engine.intraday_pipeline import IntradayPipeline
from xauusd.strategy.intraday.expansion_pullback import SETUP_TF, TRIGGER_TF

SPEC = SymbolSpec(
    "XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5, commission_per_lot=7.0
)
NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def settings(**intraday) -> Settings:
    base = {"enabled": True}
    base.update(intraday)
    return Settings(intraday=base)


def account(equity: float = 100_000.0) -> AccountState:
    return AccountState(1, "USD", equity, equity, 0.0, equity, 0.0)


@pytest.fixture(scope="module")
def snapshot():
    data = market_m1(12_000, seed=4)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]
    bar = m1.bar_at(len(m1) - 2)
    now = bar.ts + timedelta(seconds=60)
    view = MarketView(source, "XAUUSD", now, Quote(now, bar.close - 0.05, bar.close + 0.05))
    return MarketAnalyzer(Settings()).analyze(view, None, None, 25.0, 25.0)


def _struct(tf, bias, last_bos=None, last_event=None):  # type: ignore[no-untyped-def]
    return TimeframeStructure(
        timeframe=tf,
        bias=bias,
        last_event=last_event,
        swings=(),
        dealing_range=None,
        last_bos=last_bos,
    )


def _aligned(base, direction: Direction = Direction.LONG):
    """H4/H1 agreeing, with an expansion on the setup TF and an M5 confirmation."""
    bias = Bias.BULLISH if direction is Direction.LONG else Bias.BEARISH
    mid = base.quote.mid
    return replace(
        base,
        structures={
            Timeframe.H4: _struct(Timeframe.H4, bias),
            Timeframe.H1: _struct(Timeframe.H1, bias),
            SETUP_TF: _struct(
                SETUP_TF,
                bias,
                last_bos=StructureEvent(
                    ts=NOW - timedelta(minutes=30),
                    timeframe=SETUP_TF,
                    kind=StructureKind.BOS,
                    direction=direction,
                    price=mid,
                    break_price=mid,
                    displacement_atr=1.5,
                ),
            ),
            TRIGGER_TF: _struct(
                TRIGGER_TF,
                bias,
                last_event=StructureEvent(
                    ts=NOW,
                    timeframe=TRIGGER_TF,
                    kind=StructureKind.BOS,
                    direction=direction,
                    price=mid,
                    break_price=mid,
                    displacement_atr=1.0,
                ),
            ),
        },
    )


def _run(pipe: IntradayPipeline, snap, **kw):  # type: ignore[no-untyped-def]
    return pipe.run(
        snap,
        account=kw.pop("account_state", account()),
        spec=SPEC,
        now=kw.pop("now", NOW),
        **kw,
    )


class TestTheEngineIsActuallyReachable:
    def test_a_disabled_engine_decides_nothing_and_says_so(self, snapshot) -> None:
        cycle = _run(IntradayPipeline(Settings()), snapshot)
        assert cycle.skipped == "intraday engine disabled"
        assert cycle.plan is None

    def test_an_enabled_engine_runs_the_sequence_and_journals_where_it_stopped(
        self, snapshot
    ) -> None:
        cycle = _run(IntradayPipeline(settings()), snapshot)
        assert cycle.skipped is None
        assert cycle.evaluation is not None
        # Whatever it reached, the journal must be able to say so by name.
        assert cycle.reached in {"context", "expansion", "pullback", "entry"}
        assert any(c.name == "intraday_setup" for c in cycle.checks)


class TestItCannotReachRealMoneyUnvalidated:
    """The check the scalp path did not have for months (BUG-001)."""

    def test_an_unvalidated_model_is_refused_in_live_mode(self, snapshot) -> None:
        from xauusd.domain.enums import Mode

        s = settings()
        pipe = IntradayPipeline(
            Settings(intraday=s.intraday.model_dump(), mode=Mode.LIVE, live_trading=True)
        )
        cycle = _run(pipe, _aligned(snapshot))
        assert cycle.rejected_by == "intraday_strategy_validated"

    def test_the_check_runs_before_anything_else(self, snapshot) -> None:
        """A gate that only runs after four others pass has never been exercised by
        the failing case."""
        from xauusd.domain.enums import Mode

        s = settings()
        pipe = IntradayPipeline(
            Settings(intraday=s.intraday.model_dump(), mode=Mode.LIVE, live_trading=True)
        )
        cycle = _run(pipe, snapshot)
        assert cycle.checks[0].name == "intraday_strategy_validated"

    def test_a_validated_model_passes_the_check(self, snapshot) -> None:
        from xauusd.domain.enums import Mode

        s = settings()
        pipe = IntradayPipeline(
            Settings(intraday=s.intraday.model_dump(), mode=Mode.LIVE, live_trading=True)
        )
        cycle = _run(
            pipe,
            snapshot,
            strategy_status={pipe.engine.name: ValidationStatus.OOS_PASSED},
        )
        assert cycle.rejected_by != "intraday_strategy_validated"


class TestTheSharedCapsBind:
    def test_the_engine_budget_is_consulted_before_the_risk_gate(self, snapshot) -> None:
        """§15: the per-engine aggregate is a real gate, not a number on a dashboard."""
        pipe = IntradayPipeline(settings())
        # An intraday position with no readable stop: risk cannot be measured, so the
        # budget must refuse rather than read it as zero.
        blind = BrokerPosition(
            7,
            "XAUUSD",
            Direction.LONG,
            0.1,
            2000.0,
            0.0,
            2020.0,
            NOW,
            magic=Settings().broker.intraday_magic,
        )
        cycle = _run(pipe, _aligned(snapshot), open_positions=[blind])
        if cycle.reached == "entry":
            assert cycle.rejected_by == "intraday_engine_budget"

    def test_it_uses_the_risk_gate_instance_it_is_given(self) -> None:
        """One choke point, not a second copy that agrees by coincidence."""
        from xauusd.risk.gate import RiskGate

        gate = RiskGate(settings())
        assert IntradayPipeline(settings(), risk_gate=gate).risk_gate is gate

    def test_intraday_has_its_own_rr_floor_and_it_is_not_the_a_plus_one(self) -> None:
        s = Settings()
        assert s.min_rr_for(Classification.INTRADAY) == s.intraday.min_rr
        assert s.min_rr_for(Classification.INTRADAY) != s.min_rr_for(Classification.A_PLUS)

    def test_intraday_risk_comes_from_its_own_config(self) -> None:
        from xauusd.risk.gate import RiskGate

        s = Settings()
        gate = RiskGate(s)
        gate.drawdown.update(NOW, 10_000.0)
        risk, caps = gate.approved_risk_pct(Classification.INTRADAY)
        assert caps["class_cap"] == s.intraday.risk_pct


class TestOneEntryPerSetupSurvivesExecution:
    def test_the_pipeline_does_not_consume_on_a_decision(self, snapshot) -> None:
        """§28 retires a setup when a trade was TAKEN. `run` does not take trades, so
        `run` must not retire anything — an order the broker refuses would otherwise
        cost the trade the setup existed for."""
        pipe = IntradayPipeline(settings())
        _run(pipe, _aligned(snapshot))
        assert pipe.engine.setup is None or not pipe.engine.setup.consumed

    def test_consume_is_what_retires_it(self, snapshot) -> None:
        pipe = IntradayPipeline(settings())
        _run(pipe, _aligned(snapshot))
        if pipe.engine.setup is not None:
            pipe.consume()
            assert pipe.engine.setup.consumed

    def test_the_engine_instance_persists_between_cycles(self, snapshot) -> None:
        """The setup memory IS what makes the sequence ordered in time. A pipeline that
        rebuilt the engine each cycle would re-derive expansion and pullback from one
        snapshot, which accepts them in either order."""
        pipe = IntradayPipeline(settings())
        first = pipe.engine
        _run(pipe, _aligned(snapshot))
        _run(pipe, _aligned(snapshot))
        assert pipe.engine is first


@pytest.fixture(scope="module")
def in_the_zone(snapshot):
    """A snapshot with price sitting inside an unmitigated M15 bullish FVG.

    The pullback step (§23) needs price to actually BE in a structurally relevant zone,
    so a fixture that never puts it there would let every "wired" test pass while the
    sequence quietly stopped one step short of ever producing a trade.
    """
    fvg = next(
        f
        for f in snapshot.fvgs
        if f.timeframe is SETUP_TF and f.direction is Direction.LONG and f.is_tradable
    )
    mid = (fvg.top + fvg.bottom) / 2
    return replace(snapshot, quote=Quote(snapshot.quote.ts, mid - 0.05, mid + 0.05))


class TestTheWholeChainCanActuallyProduceATrade:
    """The test that would have caught "built and connected to nothing" at any seam.

    Everything above proves the parts are joined. This proves a setup can travel the
    entire distance — sequence, budget, gate, sizing — and come out the far end as a
    sized order. A pipeline where every stage is reachable but no input ever clears all
    of them is still a pipeline that trades nothing.
    """

    def test_a_complete_setup_is_approved_and_sized(self, in_the_zone) -> None:
        # No resting liquidity ahead, so §26's structural target falls back to the
        # configured RR rather than to whatever the synthetic data happens to hold.
        snap = replace(_aligned(in_the_zone), liquidity=())
        cycle = _run(IntradayPipeline(settings()), snap)
        assert cycle.reached == "entry"
        assert cycle.approved, f"refused by {cycle.rejected_by}"
        assert cycle.volume > 0
        assert cycle.risk_pct == pytest.approx(Settings().intraday.risk_pct)
        assert cycle.plan is not None and cycle.plan.rr >= Settings().intraday.min_rr

    def test_a_level_too_close_to_trade_to_no_longer_vetoes_the_setup(self, in_the_zone) -> None:
        """The defect this engine shipped with (FINDINGS 45).

        `in_the_zone` carries real nearby liquidity. Under the original rule the nearest
        pool — an M15 micro-level minutes away — became THE target, the plan came out
        below the 1.5 floor, and the setup was refused. The nearest level the engine is
        actually permitted to trade to is the target now, so a level it may not aim at
        can no longer veto a setup that had a usable one further out.
        """
        cycle = _run(IntradayPipeline(settings()), _aligned(in_the_zone))
        assert cycle.reached == "entry", "the sequence must complete for this to be a test"
        assert cycle.approved, f"refused by {cycle.rejected_by}"
        assert cycle.plan is not None
        assert cycle.plan.rr >= Settings().intraday.min_rr

    def test_the_target_is_a_real_level_and_not_an_invented_price(self, in_the_zone) -> None:
        """Skipping a level is allowed; inventing one is not. Whatever the engine aims
        at must be either a resting liquidity price that exists in the snapshot, or the
        stated fallback R:R — and the journal must be able to say which."""
        snap = _aligned(in_the_zone)
        cycle = _run(IntradayPipeline(settings()), snap)
        assert cycle.plan is not None
        target = cycle.plan.targets[0].price
        entry, stop = cycle.plan.entry, cycle.plan.stop_loss
        fallback = entry + abs(entry - stop) * Settings().intraday.fallback_target_rr
        real_levels = {p.price for p in snap.liquidity if p.is_resting}
        assert target in real_levels or target == pytest.approx(fallback)

    def test_the_floor_is_still_enforced_at_the_gate(self, in_the_zone) -> None:
        """Defence in depth. The target rule cannot produce a sub-floor plan, but
        slippage at execution can, so the gate keeps its own check rather than trusting
        the strategy to have got it right."""
        from xauusd.risk.gate import RiskGate

        gate = RiskGate(settings())
        gate.drawdown.update(NOW, 100_000.0)
        cycle = _run(IntradayPipeline(settings()), _aligned(in_the_zone))
        assert cycle.plan is not None
        # The same plan with its target dragged inside the floor, as slippage would.
        from xauusd.domain.types import TargetLevel

        plan = cycle.plan
        risk = abs(plan.entry - plan.stop_loss)
        near = plan.entry + risk * 0.5
        tight = replace(plan, targets=(TargetLevel(near, 0.5, "dragged in"),))
        assert tight.rr < Settings().intraday.min_rr
        decision = gate.evaluate(
            tight, Classification.INTRADAY, account(), SPEC, NOW, engine="intraday"
        )
        assert not decision.approved
        assert "risk.min_rr" in decision.failed

    def test_a_scalp_position_in_the_same_direction_does_not_block_it(self, in_the_zone) -> None:
        """The dual-engine case, end to end (§15). Two engines, one symbol, one
        direction, two independent stops — bounded by the budgets, not by refusing."""
        s = Settings()
        snap = replace(_aligned(in_the_zone), liquidity=())
        scalp_pos = BrokerPosition(
            11,
            "XAUUSD",
            Direction.LONG,
            0.01,
            2000.0,
            1999.0,
            2002.0,
            NOW,
            magic=s.broker.scalp_magic,
        )
        cycle = _run(IntradayPipeline(settings()), snap, open_positions=[scalp_pos])
        assert "risk.no_stacking" not in [c.name for c in cycle.checks if not c.passed]
        assert cycle.approved, f"refused by {cycle.rejected_by}"

    def test_its_own_open_position_does_block_it(self, in_the_zone) -> None:
        """§28 is one at a time, and averaging stays impossible."""
        s = Settings()
        snap = replace(_aligned(in_the_zone), liquidity=())
        mine = BrokerPosition(
            12,
            "XAUUSD",
            Direction.LONG,
            0.01,
            2000.0,
            1999.0,
            2002.0,
            NOW,
            magic=s.broker.intraday_magic,
        )
        cycle = _run(IntradayPipeline(settings()), snap, open_positions=[mine])
        assert not cycle.approved
        assert cycle.rejected_by == "intraday_engine_budget"


class TestBothRunnersDriveIt:
    """The asymmetry guard. An engine wired into one runner and not the other either
    trades unvalidated or validates something that never trades."""

    def test_the_live_orchestrator_runs_the_intraday_engine(self) -> None:
        from xauusd.engine import orchestrator

        src = inspect.getsource(orchestrator.TradingEngine)
        assert "IntradayPipeline(" in src, "the live engine must construct it"
        assert "self.intraday.run(" in src, "the live engine must actually run it"
        assert "self.intraday.consume()" in src, "§28 must be enforced on a live fill"

    def test_the_backtester_runs_the_intraday_engine(self) -> None:
        from xauusd.backtesting import engine as bt

        src = inspect.getsource(bt.BacktestEngine)
        assert "IntradayPipeline(" in src, "the backtester must construct it"
        assert "intraday.run(" in src, "the backtester must actually run it"
        assert "intraday.consume()" in src, "§28 must be enforced in the backtest too"

    def test_both_runners_consult_the_regime_controller(self) -> None:
        """§31 removing permission live but not in the backtest would make every
        validation number describe a system that is not the one trading."""
        from xauusd.backtesting import engine as bt
        from xauusd.engine import orchestrator

        live = inspect.getsource(orchestrator.TradingEngine)
        back = inspect.getsource(bt.BacktestEngine)
        for src in (live, back):
            assert "intraday_allowed" in src
            assert "scalp_allowed" in src


class TestEngineAttributionIsOneMapping:
    def test_the_magic_an_engine_stamps_is_the_magic_it_is_recognised_by(self) -> None:
        s = Settings()
        assert s.engine_magic("intraday") == s.broker.intraday_magic
        assert s.engine_magic("scalp") == s.broker.scalp_magic
        assert s.engine_magic("") == s.broker.magic
        assert s.engine_magic(s.engine_for(Classification.INTRADAY)) == s.broker.intraday_magic
        assert s.engine_magic(s.engine_for(Classification.SCALP)) == s.broker.scalp_magic
        assert s.engine_magic(s.engine_for(Classification.A_PLUS)) == s.broker.magic

    def test_every_engine_magic_is_owned(self) -> None:
        """The reconciler asks `owned_magics`. A magic we stamp but do not own would be
        reported as a stranger trading our account — a CRITICAL divergence about our
        own position."""
        s = Settings()
        for engine in ("", "scalp", "intraday"):
            assert s.engine_magic(engine) in s.owned_magics()


class TestTheJournalSaysWhereTheTargetCameFrom:
    """Fifteen of nineteen targets on synthetic data came from the fallback R:R, not
    from structure (FINDINGS 45). A plan that labels all of them "major liquidity" is
    telling a story about structure that was never there — and it is the common case,
    not the edge case, so the label has to be earned."""

    def test_a_structural_target_says_so(self, in_the_zone) -> None:
        cycle = _run(IntradayPipeline(settings()), _aligned(in_the_zone))
        assert cycle.plan is not None
        snap = _aligned(in_the_zone)
        target = cycle.plan.targets[0].price
        if target in {p.price for p in snap.liquidity if p.is_resting}:
            assert "liquidity" in cycle.plan.targets[0].rationale

    def test_a_fallback_target_admits_it(self, in_the_zone) -> None:
        snap = replace(_aligned(in_the_zone), liquidity=())
        cycle = _run(IntradayPipeline(settings()), snap)
        assert cycle.plan is not None
        rationale = cycle.plan.targets[0].rationale
        assert "fallback" in rationale
        assert "liquidity" not in rationale

    def test_the_source_travels_into_the_evidence(self, in_the_zone) -> None:
        """The journal is read long after the cycle is gone; the reason has to be in
        the record rather than in a log line that scrolled away."""
        snap = replace(_aligned(in_the_zone), liquidity=())
        cycle = _run(IntradayPipeline(settings()), snap)
        assert cycle.plan is not None
        assert "fallback" in str(cycle.plan.evidence["target_source"])
