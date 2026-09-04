"""Transaction costs, and the gates that refuse a trade they would consume.

This is the arithmetic the whole scalp premise rests on. The A/A+ engine can afford to
ignore costs because it risks dollars to make dollars over hours; a scalp risking $2 to
make $3 cannot, because the round trip costs $0.62 of that $2.

Every number below was hand-derived from the broker's reported spec — 100oz contract,
$7/lot commission — so a change in the model that shifts them shows up here rather than
in a backtest that quietly gets better.
"""

from __future__ import annotations

import pytest

from xauusd.domain.types import SymbolSpec
from xauusd.risk.cost_model import CostModel
from xauusd.strategy.scalp_gates import ScalpEconomics, evaluate_economics


def gold() -> SymbolSpec:
    return SymbolSpec("XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)


def model(max_spread: float = 50.0) -> CostModel:
    return CostModel(
        gold(), commission_per_lot=7.0, slippage_points=15.0, max_spread_points=max_spread
    )


NORMAL = 25.0  # points — a typical London spread


class TestTheArithmetic:
    def test_costs_decompose_as_expected(self) -> None:
        c = model().costs(2.00, spread_points=NORMAL)
        assert c.spread == pytest.approx(0.25)  # 25 points
        assert c.entry_slippage == pytest.approx(0.15)
        assert c.exit_slippage == pytest.approx(0.15)
        assert c.commission == pytest.approx(0.07)  # $7 per 100oz lot

    def test_the_winning_side_does_not_pay_exit_slippage(self) -> None:
        """A take-profit is a resting limit: it fills at the price or not at all."""
        c = model().costs(2.00, spread_points=NORMAL)
        assert c.cost_on_win == pytest.approx(0.47)
        assert c.cost_on_loss == pytest.approx(0.62)
        assert c.cost_on_loss > c.cost_on_win

    def test_cost_as_a_share_of_risk_scales_with_the_stop(self) -> None:
        m = model()
        assert m.costs(0.30, NORMAL).as_fraction_of_risk == pytest.approx(2.07, abs=0.01)
        assert m.costs(2.00, NORMAL).as_fraction_of_risk == pytest.approx(0.31, abs=0.01)
        assert m.costs(5.00, NORMAL).as_fraction_of_risk == pytest.approx(0.124, abs=0.01)


class TestATightStopIsMathematicallyDead:
    """A 30-point stop costs more than it risks. This is the finding that shaped the
    whole design: it is not a tuning problem, and no entry logic recovers it."""

    def test_costs_exceed_the_risk_entirely(self) -> None:
        c = model().costs(0.30, spread_points=NORMAL)
        assert c.as_fraction_of_risk > 1.0

    def test_no_win_rate_saves_it(self) -> None:
        c = model().costs(0.30, spread_points=NORMAL)
        assert c.net_rr(1.5) < 0
        assert c.break_even_win_rate(1.5) == float("inf")
        # Even a 95% win rate loses money.
        assert c.net_expectancy_r(1.5, 0.95) < 0

    def test_a_structural_stop_is_viable(self) -> None:
        c = model().costs(3.00, spread_points=NORMAL)
        assert c.as_fraction_of_risk < 0.25
        assert c.break_even_win_rate(1.5) < 0.50


class TestAMissingSpreadNeverFlatters:
    """Degradation is one-directional: an unknown execution condition must make the
    system less willing to trade, never more."""

    def test_an_unreadable_spread_assumes_the_maximum(self) -> None:
        m = model(max_spread=50.0)
        unknown = m.costs(2.00, spread_points=None)
        known = m.costs(2.00, spread_points=NORMAL)
        assert unknown.total > known.total
        assert unknown.spread == pytest.approx(0.50)


class TestTheGatesRefuseTheRightTrades:
    def test_a_tight_stop_fails_the_cost_ratio(self) -> None:
        e = ScalpEconomics(
            model(), stop_distance=0.60, gross_rr=1.5, win_probability=0.65, spread_points=NORMAL
        )
        results = {r.name: r for r in evaluate_economics(e)}
        assert not results["scalp_cost_ratio"].passed
        assert "would be needed" in results["scalp_cost_ratio"].detail

    def test_the_rejection_says_what_stop_would_work(self) -> None:
        """A refusal an operator can act on beats one they can only observe."""
        e = ScalpEconomics(
            model(), stop_distance=0.60, gross_rr=1.5, win_probability=0.65, spread_points=NORMAL
        )
        detail = evaluate_economics(e)[0].detail
        # 0.62 of cost against the 35% ceiling needs a 1.77 stop; the setup offers 0.60.
        assert "1.77" in detail

    def test_a_structural_stop_passes_both_gates(self) -> None:
        e = ScalpEconomics(
            model(), stop_distance=3.00, gross_rr=1.5, win_probability=0.65, spread_points=NORMAL
        )
        assert all(r.passed for r in evaluate_economics(e))

    def test_a_low_win_probability_fails_expectancy_even_on_a_wide_stop(self) -> None:
        e = ScalpEconomics(
            model(), stop_distance=3.00, gross_rr=1.5, win_probability=0.40, spread_points=NORMAL
        )
        results = {r.name: r for r in evaluate_economics(e)}
        assert results["scalp_cost_ratio"].passed, "the stop itself is fine"
        assert not results["scalp_net_expectancy"].passed, "but the edge is not"

    def test_the_cost_gate_ignores_the_win_probability(self) -> None:
        """A confident probability estimate must not license a trade whose economics
        only work if that estimate is right."""
        tight = dict(stop_distance=0.60, gross_rr=1.5, spread_points=NORMAL)
        pessimistic = ScalpEconomics(model(), win_probability=0.50, **tight)
        optimistic = ScalpEconomics(model(), win_probability=0.95, **tight)
        assert not evaluate_economics(pessimistic)[0].passed
        assert not evaluate_economics(optimistic)[0].passed

    def test_a_wide_spread_can_reject_a_setup_that_would_pass_in_london(self) -> None:
        """The same setup, the same stop, a different hour. This is what replaces the
        session whitelist: an economic judgement per bar, not a calendar rule."""
        london = ScalpEconomics(
            model(), stop_distance=2.50, gross_rr=1.5, win_probability=0.65, spread_points=20.0
        )  # 23% of 1R
        rollover = ScalpEconomics(
            model(), stop_distance=2.50, gross_rr=1.5, win_probability=0.65, spread_points=200.0
        )  # 95% of 1R
        assert all(r.passed for r in evaluate_economics(london))
        assert not all(r.passed for r in evaluate_economics(rollover))


class TestSmallTargetsAreAllowedWhenTheyClearCosts:
    """The point of the whole exercise: 1:1.25 is permitted where 1:2 might not be."""

    def test_a_small_target_on_a_wide_stop_passes(self) -> None:
        e = ScalpEconomics(
            model(), stop_distance=4.00, gross_rr=1.25, win_probability=0.65, spread_points=NORMAL
        )
        assert all(r.passed for r in evaluate_economics(e))

    def test_a_large_target_whose_costs_eat_it_still_fails(self) -> None:
        """Stricter than min_rr 2.0 where it matters: a 1:2 setup is not automatically
        acceptable if the stop is too tight for the spread."""
        e = ScalpEconomics(
            model(), stop_distance=0.50, gross_rr=2.0, win_probability=0.65, spread_points=NORMAL
        )
        assert not all(r.passed for r in evaluate_economics(e))
