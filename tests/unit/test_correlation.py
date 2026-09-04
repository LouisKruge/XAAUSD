"""Correlation budgets.

Ten open XAUUSD scalps are one bet on gold held ten times. Monte Carlo over 200,000
clusters: independent signals never exhaust the 2% daily limit; at a realistic
correlation of 0.7 a single losing cluster does so on 14% of days; when every position
comes from the same signal in the same direction, on 32%. Mean cluster return is
positive in all three — the failure is concentrated variance, not negative edge.

These budgets are what make concurrency above one defensible, so they are pinned here
and `ScalpConfig` refuses `max_concurrent > 1` without them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Direction
from xauusd.risk.correlation import (
    CorrelationLimits,
    OpenExposure,
    evaluate_correlation,
)

NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
ATR = 2.0


def position(
    direction: Direction = Direction.LONG,
    risk: float = 0.0015,
    stop: float = 2600.0,
    model: str = "scalp_sweep_reversal",
    pool: float | None = None,
    minutes_ago: float = 30.0,
) -> OpenExposure:
    return OpenExposure(
        direction=direction,
        risk_pct=risk,
        stop_price=stop,
        opened_at=NOW - timedelta(minutes=minutes_ago),
        model=model,
        liquidity_ref=pool,
    )


def judge(
    open_positions,
    *,
    direction=Direction.LONG,
    stop=2650.0,
    risk=0.0015,
    model="scalp_fvg_retracement",
    pool=None,
    limits=None,
):
    return evaluate_correlation(
        direction=direction,
        risk_pct=risk,
        stop_price=stop,
        now=NOW,
        open_positions=open_positions,
        limits=limits or CorrelationLimits(),
        atr=ATR,
        model=model,
        liquidity_ref=pool,
    )


class TestAnEmptyBookApprovesAnything:
    def test_the_first_trade_passes_every_budget(self) -> None:
        d = judge([])
        assert d.approved
        assert not d.blocking
        assert len(d.checks) == 4, "every budget is evaluated and recorded"


class TestSameDirectionBudget:
    def test_a_diversified_book_is_allowed(self) -> None:
        book = [position(Direction.LONG, stop=2600.0), position(Direction.SHORT, stop=2700.0)]
        assert judge(book, stop=2650.0).approved

    def test_too_much_facing_one_way_is_refused(self) -> None:
        """60% of a 2% cap is 1.2%; eight longs at 0.15% is exactly that."""
        book = [position(Direction.LONG, stop=2600.0 + i * 20) for i in range(8)]
        d = judge(book, direction=Direction.LONG, stop=2900.0)
        assert not d.approved
        assert "corr.same_direction" in d.blocking

    def test_the_opposite_direction_is_still_available(self) -> None:
        book = [position(Direction.LONG, stop=2600.0 + i * 20) for i in range(8)]
        assert judge(book, direction=Direction.SHORT, stop=2900.0).approved

    def test_the_budget_is_a_fraction_of_the_global_cap(self) -> None:
        """Raising the global cap must not silently widen a correlation budget by a
        different proportion."""
        tight = CorrelationLimits(max_total_open_risk_pct=0.01)
        assert tight.same_direction_cap == pytest.approx(0.006)


class TestSameZoneBudget:
    """Two stops within an ATR are one position: the move that takes one takes both."""

    def test_a_stop_in_the_same_zone_is_refused(self) -> None:
        d = judge([position(stop=2600.0)], stop=2601.0)  # 0.5 ATR apart
        assert not d.approved
        assert "corr.same_zone" in d.blocking

    def test_the_rejection_names_both_stops(self) -> None:
        d = judge([position(stop=2600.0)], stop=2601.0)
        detail = next(c.detail for c in d.checks if c.name == "corr.same_zone")
        assert "2600.00" in detail and "2601.00" in detail

    def test_a_distant_stop_is_allowed(self) -> None:
        assert judge([position(stop=2600.0)], stop=2610.0).approved

    def test_the_zone_scales_with_atr(self) -> None:
        """A wider market means wider zones; a fixed price distance would be wrong in
        both directions as volatility changes."""
        quiet = evaluate_correlation(
            direction=Direction.LONG,
            risk_pct=0.0015,
            stop_price=2602.0,
            now=NOW,
            open_positions=[position(stop=2600.0)],
            limits=CorrelationLimits(),
            atr=0.5,
            model="m",
        )
        volatile = evaluate_correlation(
            direction=Direction.LONG,
            risk_pct=0.0015,
            stop_price=2602.0,
            now=NOW,
            open_positions=[position(stop=2600.0)],
            limits=CorrelationLimits(),
            atr=5.0,
            model="m",
        )
        assert quiet.approved, "2 apart is far in a quiet market"
        assert not volatile.approved, "2 apart is the same zone in a volatile one"


class TestSamePoolBudget:
    def test_two_trades_on_one_liquidity_event_are_refused(self) -> None:
        d = judge([position(pool=2580.0, stop=2570.0)], stop=2650.0, pool=2580.2)
        assert not d.approved
        assert "corr.same_pool" in d.blocking

    def test_different_pools_are_independent(self) -> None:
        assert judge([position(pool=2580.0, stop=2570.0)], stop=2650.0, pool=2620.0).approved

    def test_an_unknown_pool_does_not_block(self) -> None:
        """Not every model is premised on a pool; absence is not a match."""
        assert judge([position(pool=None, stop=2570.0)], stop=2650.0, pool=None).approved


class TestSameSetupBurst:
    def test_the_same_model_firing_twice_quickly_is_one_signal(self) -> None:
        book = [position(model="scalp_sweep_reversal", minutes_ago=1, stop=2500.0)]
        d = judge(book, model="scalp_sweep_reversal", stop=2650.0)
        assert not d.approved
        assert "corr.same_setup" in d.blocking

    def test_the_same_model_later_is_allowed(self) -> None:
        book = [position(model="scalp_sweep_reversal", minutes_ago=30, stop=2500.0)]
        assert judge(book, model="scalp_sweep_reversal", stop=2650.0).approved

    def test_a_different_model_in_the_window_is_allowed(self) -> None:
        book = [position(model="scalp_sweep_reversal", minutes_ago=1, stop=2500.0)]
        assert judge(book, model="scalp_ob_reaction", stop=2650.0).approved

    def test_too_many_from_one_model_is_refused_regardless_of_timing(self) -> None:
        book = [
            position(model="scalp_ob_reaction", minutes_ago=60 + i * 10, stop=2400.0 + i * 30)
            for i in range(3)
        ]
        d = judge(book, model="scalp_ob_reaction", stop=2900.0, direction=Direction.SHORT)
        assert not d.approved
        assert "corr.same_setup" in d.blocking


class TestTheTraceIsComplete:
    def test_every_budget_is_recorded_even_after_one_fails(self) -> None:
        """The journal must answer 'what else would have blocked this?'"""
        book = [position(stop=2600.0, pool=2580.0, model="scalp_fvg_retracement", minutes_ago=1)]
        d = judge(book, stop=2600.5, pool=2580.1, model="scalp_fvg_retracement")
        assert len(d.checks) == 4
        assert len(d.blocking) >= 3, "several budgets fail at once and all are named"

    def test_a_refusal_carries_an_actionable_reason(self) -> None:
        d = judge([position(stop=2600.0)], stop=2600.5)
        assert d.reason
        assert not d.approved


class TestConcurrencyIsNowAllowedButStillBounded:
    """The lock lifted once ScalpPipeline actually consulted these budgets on every
    candidate. What replaced it is arithmetic rather than a flag: N positions at
    risk_pct each must fit inside the unchanged 2% global cap, because that is what
    stops a correlated cluster becoming one leveraged bet."""

    def test_overlapping_positions_are_permitted(self) -> None:
        assert Settings(scalp={"max_concurrent": 5, "risk_pct": 0.0015}).scalp.max_concurrent == 5

    def test_a_book_that_breaches_the_global_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="breaches the 2% global cap"):
            Settings(scalp={"max_concurrent": 20, "risk_pct": 0.0015})

    def test_raising_risk_shrinks_the_permitted_book(self) -> None:
        """The two settings trade against each other; neither can be raised alone."""
        assert Settings(scalp={"max_concurrent": 4, "risk_pct": 0.005}).scalp.max_concurrent == 4
        with pytest.raises(ValueError, match="breaches"):
            Settings(scalp={"max_concurrent": 5, "risk_pct": 0.005})
