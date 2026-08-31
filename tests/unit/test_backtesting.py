"""Backtesting: metrics, Monte Carlo, walk-forward and the deployment gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from xauusd.backtesting import monte_carlo
from xauusd.backtesting.metrics import (
    Metrics,
    compute,
    max_drawdown,
    risk_of_ruin,
    wilson_interval,
)
from xauusd.backtesting.validation import DeploymentGate
from xauusd.backtesting.walk_forward import WalkForwardResult, Window
from xauusd.domain.enums import Classification, Direction, ExitReason, Regime, Session
from xauusd.domain.types import ClosedTrade

UTC = UTC
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def trade(r: float, i: int = 0, risk: float = 100.0, **kw) -> ClosedTrade:  # type: ignore[no-untyped-def]
    base = dict(
        opened_at=T0 + timedelta(hours=i * 6),
        closed_at=T0 + timedelta(hours=i * 6 + 3),
        symbol="XAUUSD",
        direction=Direction.LONG,
        strategy="s",
        classification=Classification.A,
        entry=2000.0,
        initial_sl=1990.0,
        exit_price=2000.0 + r * 10,
        volume=0.1,
        risk_money=risk,
        gross_pnl=r * risk,
        commission=0.0,
        swap=0.0,
        exit_reason=ExitReason.TAKE_PROFIT if r > 0 else ExitReason.STOP_LOSS,
        session=Session.LONDON,
        regime=Regime.RANGE,
    )
    base.update(kw)
    return ClosedTrade(**base)  # type: ignore[arg-type]


class TestWilson:
    def test_small_samples_have_wide_intervals(self) -> None:
        lo, hi = wilson_interval(7, 10)
        assert lo < 0.45 and hi > 0.85

    def test_the_bound_tightens_with_sample_size(self) -> None:
        widths = [wilson_interval(int(0.7 * n), n) for n in (10, 50, 100, 500, 1000)]
        spans = [hi - lo for lo, hi in widths]
        assert spans == sorted(spans, reverse=True)

    def test_exactly_70_percent_never_clears_a_70_percent_lower_bound(self) -> None:
        """The arithmetic behind the gate's documented compromise.

        Even 700 wins in 1000 trades has a lower bound of 67.1%, so a gate written
        purely as 'lower bound >= 70%' would mean 'never deploy'.
        """
        for n in (10, 50, 100, 200, 500, 1000, 2000):
            lo, _ = wilson_interval(int(0.7 * n), n)
            assert lo < 0.70


class TestMetrics:
    def test_breakevens_count_as_neither_wins_nor_losses(self) -> None:
        """Counting them either way would flatter or penalise a break-even stop."""
        trades = [trade(2.0, 0), trade(-1.0, 1), trade(0.0, 2)]
        m = compute(trades)
        assert m.trades == 3 and m.wins == 1 and m.losses == 1 and m.breakevens == 1
        assert m.win_rate == pytest.approx(0.5)  # 1 of 2 DECIDED trades

    def test_expectancy_and_profit_factor(self) -> None:
        trades = [trade(2.0, i) for i in range(6)] + [trade(-1.0, i + 6) for i in range(4)]
        m = compute(trades)
        assert m.expectancy_r == pytest.approx(0.8)
        assert m.profit_factor == pytest.approx(3.0)
        assert m.win_rate == pytest.approx(0.6)

    def test_drawdown_from_the_equity_curve(self) -> None:
        assert max_drawdown([100, 120, 90, 110])[0] == pytest.approx(0.25)

    def test_consecutive_losses(self) -> None:
        seq = [2.0, -1.0, -1.0, -1.0, 2.0, -1.0]
        m = compute([trade(r, i) for i, r in enumerate(seq)])
        assert m.max_consecutive_losses == 3

    def test_grouping_carries_its_own_confidence_bound(self) -> None:
        trades = [trade(2.0, i, session=Session.LONDON) for i in range(20)]
        trades += [trade(-1.0, i + 20, session=Session.ASIA) for i in range(10)]
        m = compute(trades)
        assert m.by_session["LONDON"]["win_rate"] == 1.0
        assert m.by_session["LONDON"]["win_rate_lower_95"] < 1.0
        assert m.by_session["ASIA"]["expectancy_r"] == pytest.approx(-1.0)

    def test_empty_input_is_safe(self) -> None:
        assert compute([]).trades == 0

    def test_risk_of_ruin_rises_with_a_negative_edge(self) -> None:
        good = risk_of_ruin(0.60, 2.0, 1.0, 0.01)
        bad = risk_of_ruin(0.25, 2.0, 1.0, 0.05)
        assert bad > good


class TestMonteCarlo:
    @pytest.fixture
    def trades(self) -> list[ClosedTrade]:
        rng = np.random.default_rng(3)
        return [trade(2.0 if rng.random() < 0.6 else -1.0, i) for i in range(150)]

    def test_shuffle_varies_drawdown_not_final_equity(self, trades) -> None:  # type: ignore[no-untyped-def]
        """Fixed-fractional compounding is order-independent, so a shuffle tells you
        about the PATH, not the destination. Reading it as an equity distribution is
        a misinterpretation worth guarding against."""
        r = monte_carlo.run(trades, simulations=300, kind="shuffle")
        assert r.final_equity_p5 == pytest.approx(r.final_equity_p95, rel=1e-6)
        assert r.max_drawdown_p95 > r.max_drawdown_mean

    def test_bootstrap_gives_a_real_distribution(self, trades) -> None:  # type: ignore[no-untyped-def]
        r = monte_carlo.run(trades, simulations=400, kind="bootstrap")
        assert r.final_equity_p5 < r.final_equity_median < r.final_equity_p95
        assert r.expectancy_p5 < r.expectancy_median

    def test_a_losing_system_shows_a_low_probability_of_profit(self) -> None:
        losers = [trade(-1.0 if i % 3 else 1.0, i) for i in range(90)]
        r = monte_carlo.run(losers, simulations=300, kind="bootstrap")
        assert r.prob_profitable < 0.1

    def test_too_few_trades_returns_a_null_result(self) -> None:
        assert monte_carlo.run([trade(1.0, 0)], simulations=100).simulations == 0


class TestWalkForward:
    def test_efficiency_detects_curve_fitting(self) -> None:
        wf = WalkForwardResult()
        for i in range(4):
            w = Window(i, T0, T0, T0, T0)
            w.is_metrics = Metrics(trades=50, expectancy_r=1.0)
            w.oos_metrics = Metrics(trades=50, expectancy_r=0.1)  # collapses OOS
            wf.windows.append(w)
        assert wf.efficiency == pytest.approx(0.1)

    def test_efficiency_near_one_means_it_carried_forward(self) -> None:
        wf = WalkForwardResult()
        for i in range(4):
            w = Window(i, T0, T0, T0, T0)
            w.is_metrics = Metrics(trades=50, expectancy_r=0.5)
            w.oos_metrics = Metrics(trades=50, expectancy_r=0.48)
            wf.windows.append(w)
        assert 0.9 < wf.efficiency < 1.1
        assert wf.profitable_window_fraction == 1.0


class TestDeploymentGate:
    def _strong_oos(self) -> Metrics:
        return Metrics(
            trades=180,
            wins=131,
            losses=49,
            win_rate=0.728,
            win_rate_wilson_lower_95=0.661,
            profit_factor=2.9,
            expectancy_r=0.82,
            max_drawdown_pct=0.09,
            avg_rr_realised=1.95,
            sharpe=2.1,
            sortino=3.0,
            max_consecutive_losses=5,
            risk_of_ruin=0.001,
        )

    def _full_evidence(self) -> dict:
        wf = WalkForwardResult()
        for i in range(6):
            w = Window(i, T0, T0, T0, T0)
            w.is_metrics = Metrics(trades=40, expectancy_r=0.9)
            w.oos_metrics = Metrics(trades=30, expectancy_r=0.7)
            wf.windows.append(w)
        mc = {
            "bootstrap": monte_carlo.MonteCarloResult(
                1000,
                14000,
                11000,
                12000,
                13500,
                17000,
                0.08,
                0.12,
                0.15,
                0.98,
                0.02,
                0.4,
                0.8,
                0.62,
                "bootstrap",
            ),
            "shuffle": monte_carlo.MonteCarloResult(
                1000,
                14000,
                14000,
                14000,
                14000,
                14000,
                0.09,
                0.14,
                0.18,
                1.0,
                0.01,
                0.8,
                0.8,
                0.7,
                "shuffle",
            ),
        }
        return {
            "walk_forward": wf,
            "monte_carlo": mc,
            "sensitivity": {"max_relative_drop": 0.3},
            "stress": {"2x": Metrics(trades=180, expectancy_r=0.31, profit_factor=1.6)},
            "calibration": {"brier": 0.19, "slope": 1.02},
            "leak_checks": {
                "no_lookahead_in_features": True,
                "vintage_filtered_macro": True,
                "masked_future_actuals": True,
                "time_ordered_split": True,
                "costs_modelled": True,
            },
        }

    def test_a_strong_strategy_with_full_evidence_passes(self) -> None:
        r = DeploymentGate().evaluate("s", "1.0", self._strong_oos(), **self._full_evidence())
        assert r.passed, [c.name for c in r.failures]
        assert "suspected data leak" in " ".join(r.notes)

    def test_a_seven_of_ten_fluke_fails(self) -> None:
        m = Metrics(
            trades=10,
            wins=7,
            losses=3,
            win_rate=0.7,
            win_rate_wilson_lower_95=wilson_interval(7, 10)[0],
            profit_factor=4.6,
            expectancy_r=1.1,
            max_drawdown_pct=0.03,
            avg_rr_realised=2.0,
            sharpe=2.0,
            sortino=3.0,
            max_consecutive_losses=2,
            risk_of_ruin=0.0,
        )
        r = DeploymentGate().evaluate("f", "1.0", m, **self._full_evidence())
        assert not r.passed
        assert "oos_sample_size" in [c.name for c in r.failures]
        assert "win_rate_lower_bound" in [c.name for c in r.failures]

    def test_a_69_percent_win_rate_fails_the_stated_bar(self) -> None:
        m = self._strong_oos()
        m.win_rate = 0.69
        r = DeploymentGate().evaluate("s", "1.0", m, **self._full_evidence())
        assert "win_rate_observed" in [c.name for c in r.failures]

    def test_missing_evidence_blocks_rather_than_being_skipped(self) -> None:
        """An unrun test is a failure, not a pass."""
        r = DeploymentGate().evaluate("s", "1.0", self._strong_oos())
        names = [c.name for c in r.failures]
        assert "walk_forward_efficiency" in names
        assert "monte_carlo_p5_equity" in names
        assert "parameter_sensitivity" in names
        assert "stress_expectancy" in names

    def test_a_failed_leak_check_blocks_deployment(self) -> None:
        ev = self._full_evidence()
        ev["leak_checks"]["vintage_filtered_macro"] = False
        r = DeploymentGate().evaluate("s", "1.0", self._strong_oos(), **ev)
        assert not r.passed
        assert "leak_check.vintage_filtered_macro" in [c.name for c in r.failures]

    def test_cost_stress_failure_blocks(self) -> None:
        ev = self._full_evidence()
        ev["stress"] = {"2x": Metrics(trades=180, expectancy_r=-0.05, profit_factor=0.9)}
        r = DeploymentGate().evaluate("s", "1.0", self._strong_oos(), **ev)
        assert "stress_expectancy" in [c.name for c in r.failures]

    def test_approved_sessions_come_from_measured_performance(self) -> None:
        m = self._strong_oos()
        m.by_session = {
            "LONDON": {
                "trades": 100,
                "expectancy_r": 0.9,
                "win_rate": 0.72,
                "win_rate_lower_95": 0.63,
                "total_r": 90,
            },
            "ASIA": {
                "trades": 40,
                "expectancy_r": -0.2,
                "win_rate": 0.4,
                "win_rate_lower_95": 0.26,
                "total_r": -8,
            },
        }
        r = DeploymentGate().evaluate("s", "1.0", m, **self._full_evidence())
        assert r.approved_sessions == ["LONDON"]

    def test_report_renders_readably(self) -> None:
        r = DeploymentGate().evaluate("s", "1.0", self._strong_oos(), **self._full_evidence())
        text = r.render()
        assert "VALIDATION REPORT" in text and "win_rate_observed" in text
