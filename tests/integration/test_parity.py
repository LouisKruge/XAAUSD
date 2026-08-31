"""Backtest / live parity.

This is the guard that keeps the two code paths from quietly drifting apart over
months of development. If it fails, no validation number can be trusted until it is
fixed, which is why it runs in CI on every push.

The claim being tested is specific: for the same instant and the same data, the
DecisionPipeline driven directly (the live path) and the DecisionPipeline driven by
the BacktestEngine produce the SAME decision — same classification, same score, same
blocking gate. Only MarketView and Broker are substituted between them.
"""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest

from tests.fixtures.synthetic import market
from xauusd.backtesting.engine import BacktestConfig, BacktestEngine, data_hash, split_data
from xauusd.config.settings import Settings, StrategyThresholds
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import Timeframe, ValidationStatus
from xauusd.domain.types import AccountState, Quote, SymbolSpec
from xauusd.engine.pipeline import DecisionPipeline, EngineState
from xauusd.execution.broker import BrokerHealth
from xauusd.execution.sim_broker import SimFillModel

UTC = UTC
STRATEGIES = ["sweep_mss_fvg", "sweep_mss_ob"]


def spec() -> SymbolSpec:
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
        commission_per_lot=7.0,
    )


def settings() -> Settings:
    return Settings(
        enabled_strategies=STRATEGIES,
        thresholds=StrategyThresholds(
            a_score_min=40.0,
            a_plus_score_min=65.0,
            a_strong_categories_min=2,
            a_plus_strong_categories_min=6,
        ),
    )


@pytest.fixture(scope="module")
def data():  # type: ignore[no-untyped-def]
    return market(20000, seed=11, drift_per_bar=0.006, noise=0.5)


class TestParity:
    def test_same_bar_produces_the_same_decision_in_both_paths(self, data) -> None:  # type: ignore[no-untyped-def]
        cfg = settings()
        m5 = data[Timeframe.M5]
        warmup = 6000
        n_bars = 400

        # --- the backtest path ---
        engine = BacktestEngine(
            cfg,
            spec(),
            BacktestConfig(warmup_bars=warmup, step=1, max_bars=n_bars),
            fill_model=SimFillModel(seed=1),
            strategy_status=dict.fromkeys(STRATEGIES, ValidationStatus.OOS_PASSED),
        )
        bt = engine.run(data)
        bt_by_ts = {}
        for d in bt.decisions:
            bt_by_ts.setdefault(d.ts, []).append(d)

        # --- the live path, driven directly with identical inputs ---
        source = InMemoryBarSource()
        for tf, s in data.items():
            source.set(tf, s)
        pipeline = DecisionPipeline(cfg)
        state = EngineState(
            account=AccountState(1, "USD", 10_000.0, 10_000.0, 0.0, 10_000.0, 0.0),
            spec=spec(),
            health=BrokerHealth(True, True, True, 0.0),
            strategy_status=dict.fromkeys(STRATEGIES, ValidationStatus.OOS_PASSED),
        )

        compared = 0
        for i in range(warmup, warmup + n_bars, 37):  # sample, not every bar
            bar = m5.bar_at(i)
            now = bar.ts + timedelta(seconds=Timeframe.M5.seconds)
            half = (bar.spread_points or 25) * spec().point / 2.0
            view = MarketView(source, "XAUUSD", now, Quote(now, bar.close - half, bar.close + half))
            live = pipeline.run(
                view,
                state,
                spread_points=float(bar.spread_points or 25),
                spread_median=engine._median_spread(m5, i),
            )
            expected = bt_by_ts.get(now)
            if expected is None:
                continue
            compared += 1
            assert len(live.decisions) == len(expected), (
                f"decision COUNT differs at {now}: live {len(live.decisions)} "
                f"vs backtest {len(expected)}"
            )
            for a, b in zip(live.decisions, expected):
                assert a.classification == b.classification, (
                    f"classification differs at {now}: {a.classification} vs {b.classification}"
                )
                if a.score is not None and b.score is not None:
                    assert a.score == pytest.approx(b.score, abs=1e-6), (
                        f"score differs at {now}: {a.score} vs {b.score}"
                    )
                assert a.blocking_gate == b.blocking_gate, (
                    f"blocking gate differs at {now}: {a.blocking_gate} vs {b.blocking_gate}"
                )
        assert compared >= 5, f"parity check only compared {compared} bars"

    def test_a_backtest_is_deterministic(self, data) -> None:  # type: ignore[no-untyped-def]
        """Same inputs, same seed, byte-identical results. Without this, no comparison
        between two runs means anything."""

        def run():  # type: ignore[no-untyped-def]
            return BacktestEngine(
                settings(),
                spec(),
                BacktestConfig(warmup_bars=6000, step=5, max_bars=1500),
                fill_model=SimFillModel(seed=42),
                strategy_status=dict.fromkeys(STRATEGIES, ValidationStatus.OOS_PASSED),
            ).run(data)

        a, b = run(), run()
        assert len(a.decisions) == len(b.decisions)
        assert len(a.trades) == len(b.trades)
        assert a.metrics.total_r == pytest.approx(b.metrics.total_r)
        assert a.data_hash == b.data_hash
        assert a.rejection_ledger == b.rejection_ledger

    def test_data_hash_detects_a_different_dataset(self, data) -> None:  # type: ignore[no-untyped-def]
        """A validation report must never silently mix data sources."""
        other = market(20000, seed=99)
        assert data_hash(data) != data_hash(other)
        assert data_hash(data) == data_hash(data)


class TestSplit:
    def test_split_is_chronological_and_non_overlapping(self, data) -> None:  # type: ignore[no-untyped-def]
        """A random split lets a model see the future of the same trend."""
        first, second = split_data(data, 0.7)
        for tf in (Timeframe.M5, Timeframe.H1, Timeframe.H4):
            a, b = first[tf], second[tf]
            if not len(a) or not len(b):
                continue
            assert a.ts[-1] < b.ts[0], f"{tf} in-sample overlaps out-of-sample"

    def test_split_covers_the_whole_dataset(self, data) -> None:  # type: ignore[no-untyped-def]
        first, second = split_data(data, 0.7)
        m5 = data[Timeframe.M5]
        assert len(first[Timeframe.M5]) + len(second[Timeframe.M5]) == len(m5)


class TestCostsAreModelled:
    def test_higher_costs_reduce_expectancy(self, data) -> None:  # type: ignore[no-untyped-def]
        """Cost stress must actually bite; if it does not, costs are not being applied."""

        def run(mult: float):  # type: ignore[no-untyped-def]
            return BacktestEngine(
                settings(),
                spec(),
                BacktestConfig(warmup_bars=6000, step=5, max_bars=4000),
                fill_model=SimFillModel(seed=7, spread_multiplier=mult, slippage_multiplier=mult),
                strategy_status=dict.fromkeys(STRATEGIES, ValidationStatus.OOS_PASSED),
            ).run(data)

        cheap, dear = run(1.0), run(3.0)
        if cheap.trades and dear.trades:
            assert dear.metrics.expectancy_r <= cheap.metrics.expectancy_r + 1e-9
        assert cheap.cost_model["spread_multiplier"] == 1.0
        assert dear.cost_model["spread_multiplier"] == 3.0
