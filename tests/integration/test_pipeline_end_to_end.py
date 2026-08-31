"""End-to-end: MarketView in, Decision out, including the path where a trade fires.

Two halves, both necessary:
  * the selective half — on data with no edge the system produces NO_TRADE and says why
  * the permissive half — given a genuine setup and supportive context, an A trade is
    produced, sized, and carries a complete audit trail

A system that only ever refuses is indistinguishable from a broken one, so the second
half matters as much as the first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.synthetic import market
from xauusd.config.settings import Settings, StrategyThresholds
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import (
    Bias,
    Classification,
    Direction,
    MacroBias,
    NewsRisk,
    Timeframe,
    ValidationStatus,
)
from xauusd.domain.types import (
    AccountState,
    MacroState,
    NewsState,
    Quote,
    SymbolSpec,
)
from xauusd.engine.pipeline import DecisionPipeline, EngineState
from xauusd.execution.broker import BrokerHealth

UTC = UTC


def gold_spec() -> SymbolSpec:
    return SymbolSpec(
        "XAUUSD",
        2,
        0.01,
        100.0,
        0.01,
        1.0,
        1.0,
        1.0,
        0.01,
        50.0,
        0.01,
        10,
        5,
        currency_profit="USD",
        commission_per_lot=7.0,
    )


def supportive_macro(direction: Direction) -> MacroState:
    """Real yields falling and the dollar weak: a bullish-gold backdrop."""
    sign = 1 if direction is Direction.LONG else -1
    return MacroState(
        bias=MacroBias.STRONGLY_BULLISH if sign > 0 else MacroBias.STRONGLY_BEARISH,
        dxy_level=103.0,
        dxy_change_1d=-0.2,
        dxy_change_5d=-0.8,
        dxy_trend=Bias.BEARISH if sign > 0 else Bias.BULLISH,
        us10y=4.1,
        us2y=4.4,
        real10y=1.85,
        real10y_change_5d=-0.12,
        breakeven10y=2.35,
        yields_trend=Bias.BEARISH if sign > 0 else Bias.BULLISH,
        curve_10y2y=-0.3,
        as_of=datetime(2026, 1, 20, tzinfo=UTC),
        is_stale=False,
    )


def calm_news() -> NewsState:
    return NewsState(
        risk=NewsRisk.LOW,
        blackout=False,
        blackout_reason=None,
        blackout_until=None,
        next_event_name="US Jobless Claims",
        next_event_ts=datetime(2026, 1, 22, 13, 30, tzinfo=UTC),
        minutes_to_next_event=2000.0,
        directional_hint=Bias.NEUTRAL,
        is_stale=False,
    )


@pytest.fixture(scope="module")
def source() -> InMemoryBarSource:
    src = InMemoryBarSource()
    for tf, s in market(12000, seed=5).items():
        src.set(tf, s)
    return src


@pytest.fixture(scope="module")
def m5(source: InMemoryBarSource):  # type: ignore[no-untyped-def]
    return source.series("XAUUSD", Timeframe.M5)


def make_state(**kw) -> EngineState:  # type: ignore[no-untyped-def]
    names = ["sweep_mss_fvg", "sweep_mss_ob", "session_range_expansion", "pdh_pdl_reversion"]
    base = dict(
        account=AccountState(1, "USD", 10_000.0, 10_000.0, 0.0, 10_000.0, 0.0),
        spec=gold_spec(),
        health=BrokerHealth(True, True, True, 0.5),
        strategy_status=dict.fromkeys(names, ValidationStatus.OOS_PASSED),
    )
    base.update(kw)
    return EngineState(**base)  # type: ignore[arg-type]


def run_window(
    source: InMemoryBarSource,
    m5,
    settings: Settings,
    macro,
    news,
    start: int,
    count: int,
    step: int = 1,
    state: EngineState | None = None,
):  # type: ignore[no-untyped-def]
    pipe = DecisionPipeline(settings)
    st = state or make_state()
    out = []
    for i in range(start, min(start + count, len(m5)), step):
        now = datetime.fromtimestamp(int(m5.ts[i]) + 300, UTC)
        px = float(m5.close[i])
        view = MarketView(source, "XAUUSD", now, Quote(now, px - 0.11, px + 0.11))
        out.append(pipe.run(view, st, macro=macro, news=news, spread_points=22, spread_median=22))
    return out


class TestSelectivity:
    """The default state is NO TRADE, and the reason is always recorded."""

    def test_no_edge_data_produces_no_trades(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        results = run_window(source, m5, Settings(), None, None, 6000, 400, step=4)
        classes = {d.classification for r in results for d in r.decisions}
        assert classes == {Classification.NO_TRADE}

    def test_every_no_trade_names_a_blocking_condition(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        """The rejection ledger must never contain an unexplained refusal."""
        results = run_window(source, m5, Settings(), None, None, 6000, 200, step=4)
        for r in results:
            for d in r.decisions:
                assert d.blocking_gate or d.reasons_against, (
                    f"decision at {d.ts} refused without a recorded reason"
                )

    def test_unknown_macro_and_stale_news_prevent_a_plus(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        """With no macro or news feed, A+ is structurally unreachable. By design."""
        results = run_window(source, m5, Settings(), None, None, 6000, 400, step=4)
        assert not any(
            d.classification is Classification.A_PLUS for r in results for d in r.decisions
        )

    def test_decision_is_produced_even_when_no_candidate_exists(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        """'The market offered nothing' must be distinguishable from 'a filter is broken'."""
        results = run_window(source, m5, Settings(), None, None, 6000, 40)
        assert all(r.decisions for r in results)
        no_candidate = [d for r in results for d in r.decisions if d.plan is None]
        assert no_candidate
        assert all(d.features for d in no_candidate)


class TestTradePathFires:
    """The positive path must work, or the system is only pretending to be selective."""

    @pytest.fixture(scope="class")
    def permissive(self) -> Settings:
        # Thresholds calibrated for the Phase 10 validation run are not yet known, so
        # this exercises the machinery at a threshold a candidate can actually reach.
        # It does NOT relax any hard gate, risk limit or the 1:2 RR floor.
        return Settings(
            enabled_strategies=[
                "sweep_mss_fvg",
                "sweep_mss_ob",
                "session_range_expansion",
                "pdh_pdl_reversion",
            ],
            thresholds=StrategyThresholds(
                a_score_min=35.0,
                a_plus_score_min=60.0,
                a_probability_min=0.5,
                a_plus_probability_min=0.6,
                a_strong_categories_min=2,
                a_plus_strong_categories_min=6,
                min_rr=2.0,
                preferred_rr=3.0,
            ),
        )

    @pytest.fixture(scope="class")
    def trades(self, source, m5, permissive):  # type: ignore[no-untyped-def]
        results = run_window(
            source,
            m5,
            permissive,
            supportive_macro(Direction.LONG),
            calm_news(),
            3000,
            6000,
            step=3,
        )
        return [d for r in results for d in r.decisions if d.is_trade]

    def test_at_least_one_trade_is_produced(self, trades) -> None:  # type: ignore[no-untyped-def]
        assert trades, "the pipeline never produced an executable decision"

    def test_every_trade_clears_the_rr_floor(self, trades) -> None:  # type: ignore[no-untyped-def]
        """The 1:2 floor is never waived, at any threshold setting."""
        assert all(d.plan.rr >= 2.0 for d in trades)

    def test_every_trade_is_sized_within_its_class_cap(self, trades) -> None:  # type: ignore[no-untyped-def]
        for d in trades:
            assert d.sizing is not None and d.sizing.approved
            cap = 0.02 if d.classification is Classification.A_PLUS else 0.01
            assert d.sizing.risk_pct <= cap + 1e-9, (
                f"{d.classification} sized at {d.sizing.risk_pct:.4%}, cap {cap:.2%}"
            )
            assert d.sizing.lots >= 0.01

    def test_every_trade_has_a_complete_audit_trail(self, trades) -> None:  # type: ignore[no-untyped-def]
        for d in trades:
            assert d.gates and all(g.passed for g in d.gates)
            assert d.breakdown is not None and d.score is not None
            assert d.features and len(d.features) > 50
            assert d.reasons_for
            assert d.plan.invalidation
            assert d.config_hash
            text = d.explain()
            assert "All gates passed" in text and str(d.classification) in text

    def test_stop_is_on_the_correct_side_and_structural(self, trades) -> None:  # type: ignore[no-untyped-def]
        for d in trades:
            p = d.plan
            if p.direction is Direction.LONG:
                assert p.stop_loss < p.entry < p.final_target.price
            else:
                assert p.stop_loss > p.entry > p.final_target.price

    def test_targets_cite_real_structure(self, trades) -> None:  # type: ignore[no-untyped-def]
        """A target must name what it is anchored to, never be a bare multiple of R."""
        for d in trades:
            for t in d.plan.targets:
                assert t.rationale and t.rationale != ""


class TestHardLimitsHoldUnderPermissiveThresholds:
    """Lowering a threshold must not be able to breach a risk limit."""

    def test_drawdown_lockout_blocks_everything(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        settings = Settings(
            thresholds=StrategyThresholds(
                a_score_min=10.0,
                a_plus_score_min=20.0,
                a_strong_categories_min=1,
                a_plus_strong_categories_min=2,
            )
        )
        pipe = DecisionPipeline(settings)
        state = make_state()
        # Drive equity down past the daily limit.
        now0 = datetime.fromtimestamp(int(m5.ts[4000]), UTC)
        pipe.risk_gate.drawdown.update(now0, 10_000.0)
        pipe.risk_gate.drawdown.update(now0 + timedelta(minutes=5), 9_700.0)
        assert pipe.risk_gate.drawdown.periods["DAY"].locked

        results = []
        for i in range(4000, 4300, 3):
            now = datetime.fromtimestamp(int(m5.ts[i]) + 300, UTC)
            px = float(m5.close[i])
            view = MarketView(source, "XAUUSD", now, Quote(now, px - 0.11, px + 0.11))
            results.append(
                pipe.run(
                    view,
                    state,
                    macro=supportive_macro(Direction.LONG),
                    news=calm_news(),
                    spread_points=22,
                    spread_median=22,
                )
            )
        assert not any(r.traded for r in results)

    def test_kill_switch_blocks_everything(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        from xauusd.domain.enums import KillSwitchReason

        settings = Settings(
            thresholds=StrategyThresholds(
                a_score_min=10.0,
                a_plus_score_min=20.0,
                a_strong_categories_min=1,
                a_plus_strong_categories_min=2,
            )
        )
        pipe = DecisionPipeline(settings)
        pipe.risk_gate.kill_switch.trip(KillSwitchReason.MANUAL, "test halt")
        state = make_state()
        for i in range(4000, 4120, 3):
            now = datetime.fromtimestamp(int(m5.ts[i]) + 300, UTC)
            px = float(m5.close[i])
            view = MarketView(source, "XAUUSD", now, Quote(now, px - 0.11, px + 0.11))
            r = pipe.run(
                view,
                state,
                macro=supportive_macro(Direction.LONG),
                news=calm_news(),
                spread_points=22,
                spread_median=22,
            )
            assert not r.traded
            assert any(
                g.name == "kill_switch" and not g.passed for d in r.decisions for g in d.gates
            )

    def test_wide_spread_blocks_entry(self, source, m5) -> None:  # type: ignore[no-untyped-def]
        settings = Settings(
            thresholds=StrategyThresholds(
                a_score_min=10.0,
                a_plus_score_min=20.0,
                a_strong_categories_min=1,
                a_plus_strong_categories_min=2,
            )
        )
        pipe = DecisionPipeline(settings)
        state = make_state()
        for i in range(4000, 4120, 3):
            now = datetime.fromtimestamp(int(m5.ts[i]) + 300, UTC)
            px = float(m5.close[i])
            view = MarketView(source, "XAUUSD", now, Quote(now, px - 0.5, px + 0.5))
            r = pipe.run(
                view,
                state,
                macro=supportive_macro(Direction.LONG),
                news=calm_news(),
                spread_points=120,
                spread_median=22,
            )
            assert not r.traded
