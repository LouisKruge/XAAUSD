"""Spent liquidity is not a barrier.

A `LiquidityPool` with `swept_ts` set has had the stops behind it taken. The rest of the
system has always encoded that: `strategy/base.py` filters `p.is_resting` when building
targets, `strategy/features.py` reads `snap.resting_liquidity(...)` for both the draw
and the opposition, and `MarketSnapshot.resting_liquidity` exists for the purpose. The
scalp obstacle set ignored it and treated every pool as a wall.

The consequence was not cosmetic. `MicroSnapshot` carries ~123 pools per instant with
90% already swept, so a target window barely one ATR wide always contained about eleven
of them. `structural_target` takes the nearest, so the target became roughly the minimum
of eleven arbitrary draws and settled just above the 0.75R noise floor — median gross
reward-to-risk 0.81 against a 1.25 floor. 82% of every signal the models produced died
on arithmetic over spent liquidity.

These tests pin the rule so it cannot quietly revert.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd.domain.enums import Direction, LevelKind, LiquidityKind, Timeframe
from xauusd.domain.types import LiquidityPool, SRLevel
from xauusd.strategy.scalp.base import structural_target
from xauusd.strategy.scalp.models import _obstacles

TS = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
ENTRY = 2000.0


def _pool(price: float, *, swept: bool, touches: int = 2) -> LiquidityPool:
    return LiquidityPool(
        kind=LiquidityKind.EQH,
        timeframe=Timeframe.M5,
        price=price,
        formed_ts=TS,
        touches=touches,
        swept_ts=TS if swept else None,
    )


def _level(price: float) -> SRLevel:
    return SRLevel(
        kind=LevelKind.RESISTANCE,
        timeframe=Timeframe.H1,
        price=price,
        band_upper=price + 0.2,
        band_lower=price - 0.2,
        formed_ts=TS,
    )


class _Micro:
    """Only the attribute `_obstacles` reads. A full MicroSnapshot would add noise."""

    def __init__(self, pools) -> None:  # type: ignore[no-untyped-def]
        self.pools = tuple(pools)


class _Snap:
    def __init__(self, levels=()) -> None:  # type: ignore[no-untyped-def]
        self.sr_levels = tuple(levels)


class TestSweptPoolsAreNotObstacles:
    def test_a_swept_pool_is_excluded(self) -> None:
        assert _obstacles(_Snap(), _Micro([_pool(2005.0, swept=True)])) == []

    def test_a_resting_pool_is_included(self) -> None:
        assert _obstacles(_Snap(), _Micro([_pool(2005.0, swept=False)])) == [2005.0]

    def test_a_realistic_mix_keeps_only_the_resting_tenth(self) -> None:
        """The measured shape: ~123 pools per snapshot, ~10% resting."""
        pools = [_pool(2000.0 + i * 0.1, swept=(i % 10 != 0)) for i in range(123)]
        assert len(_obstacles(_Snap(), _Micro(pools))) == 13

    def test_structural_levels_are_unaffected(self) -> None:
        """Only the liquidity rule changed. S/R has no swept/resting distinction."""
        assert _obstacles(_Snap([_level(2005.0)]), _Micro([])) == [2005.0]

    def test_htf_obstacles_are_passed_through(self) -> None:
        assert _obstacles(_Snap(), _Micro([]), (2006.0, 2007.0)) == [2006.0, 2007.0]


class TestTheEffectOnTargets:
    def test_a_dense_field_of_swept_pools_no_longer_collapses_the_target(self) -> None:
        """The exact failure: a wall of spent liquidity inside the target window.

        Every one of these pools sits between the 0.75R floor and the 1.5R target, so
        under the old rule the nearest of them became the target and the signal was then
        refused by `min_gross_rr`. None of them is a barrier to anything.
        """
        entry, stop = 2000.0, 1998.0  # risk 2.0 -> floor 1.5R at 2003.0, target at 2003.0
        swept = [_pool(2001.6 + i * 0.05, swept=True) for i in range(11)]
        target, rationale = structural_target(
            entry, stop, Direction.LONG, 1.5, _obstacles(_Snap(), _Micro(swept))
        )
        assert target == pytest.approx(2003.0)
        assert (target - entry) / (entry - stop) == pytest.approx(1.5)
        assert "no obstacle" in rationale

    def test_one_resting_pool_in_the_window_still_pulls_the_target(self) -> None:
        """The rule must not become "ignore liquidity". Unswept levels still count."""
        entry, stop = 2000.0, 1998.0
        pools = [_pool(2001.6 + i * 0.05, swept=True) for i in range(11)]
        pools.append(_pool(2002.4, swept=False))
        target, rationale = structural_target(
            entry, stop, Direction.LONG, 1.5, _obstacles(_Snap(), _Micro(pools))
        )
        assert target == pytest.approx(2002.4)
        assert "opposing level" in rationale

    def test_the_same_holds_for_a_short(self) -> None:
        entry, stop = 2000.0, 2002.0
        swept = [_pool(1998.4 - i * 0.05, swept=True) for i in range(11)]
        target, _ = structural_target(
            entry, stop, Direction.SHORT, 1.5, _obstacles(_Snap(), _Micro(swept))
        )
        assert target == pytest.approx(1997.0)


class TestItAgreesWithTheRestOfTheSystem:
    def test_the_scalp_rule_matches_marketsnapshot_resting_liquidity(self) -> None:
        """One definition of "still has liquidity behind it", not two.

        `MarketSnapshot.resting_liquidity` is the system's answer, and the scalp path
        having its own would be the defect class this project keeps hitting: a rule with
        several enforcement points, only some of which learn about a change.
        """
        pools = [_pool(2000.0 + i, swept=(i % 2 == 0)) for i in range(10)]
        kept = _obstacles(_Snap(), _Micro(pools))
        expected = [p.price for p in pools if p.is_resting]
        assert kept == expected


class TestTargetsMustBeReachableInsideTheHoldingWindow:
    """A target the clock cannot deliver is a time stop wearing a target's name.

    Measured on real harvested history: win rate 40.6% against a 36.4% breakeven at
    RR 1.75 — a POSITIVE gross edge — yet net expectancy -0.136R. The gap between the
    two grew with the target (0.157R at 1.5R, 0.253R at 1.75R, 0.377R at 2.0R), and
    costs do not scale with target distance. That is the signature of winners running
    out of clock, not of bad setups.

    Price covers distance with the square root of time, so a target k ATR away needs
    about k^2 five-minute bars. A flat 90-minute window reaches ~4.24 ATR — while a
    3.5 ATR stop at 2.0R aims 7.0 ATR away and needs about 245 minutes. Those trades
    could never pay out.
    """

    def test_the_reachable_distance_follows_the_square_root_of_time(self) -> None:
        from xauusd.config.settings import Settings

        s = Settings()
        atr = 1.0
        reach = s.reachable_target_distance(atr)
        assert reach is not None
        # 90 minutes = 18 M5 bars -> sqrt(18) = 4.24 ATR
        assert reach == pytest.approx((90 / 5) ** 0.5, rel=1e-6)

    def test_a_longer_window_reaches_further_but_only_as_a_square_root(self) -> None:
        """Four times the time buys twice the distance, not four times."""
        from xauusd.config.settings import Settings

        base = Settings()
        short = base.model_copy(
            update={"scalp": base.scalp.model_copy(update={"max_hold_minutes": 30})}
        )
        long = base.model_copy(
            update={"scalp": base.scalp.model_copy(update={"max_hold_minutes": 120})}
        )
        assert long.reachable_target_distance(1.0) == pytest.approx(
            2.0 * short.reachable_target_distance(1.0), rel=1e-6
        )

    def test_an_unreachable_target_is_pulled_in(self) -> None:
        entry, stop = 2000.0, 1996.5  # 3.5 risk; at 2.0R the target is 7.0 away
        target, rationale = structural_target(
            entry, stop, Direction.LONG, 2.0, [], max_distance=4.24
        )
        assert target == pytest.approx(2004.24)
        assert "capped" in rationale
        assert "holding window" in rationale

    def test_a_reachable_target_is_left_alone(self) -> None:
        entry, stop = 2000.0, 1999.0  # 1.0 risk; at 1.5R the target is 1.5 away
        target, rationale = structural_target(
            entry, stop, Direction.LONG, 1.5, [], max_distance=4.24
        )
        assert target == pytest.approx(2001.5)
        assert "capped" not in rationale

    def test_the_cap_never_pushes_a_target_further_out(self) -> None:
        """It is a ceiling, not a setting. A generous window must not widen targets."""
        entry, stop = 2000.0, 1999.0
        plain, _ = structural_target(entry, stop, Direction.LONG, 1.5, [])
        for reach in (0.5, 1.5, 5.0, 100.0):
            with_cap, _ = structural_target(
                entry, stop, Direction.LONG, 1.5, [], max_distance=reach
            )
            assert with_cap <= plain

    def test_the_mirror_holds_for_a_short(self) -> None:
        entry, stop = 2000.0, 2003.5
        target, rationale = structural_target(
            entry, stop, Direction.SHORT, 2.0, [], max_distance=4.24
        )
        assert target == pytest.approx(1995.76)
        assert "capped" in rationale

    def test_disabling_the_cap_restores_the_old_behaviour(self) -> None:
        from xauusd.config.settings import Settings

        base = Settings()
        off = base.model_copy(
            update={"scalp": base.scalp.model_copy(update={"cap_target_to_holding_window": False})}
        )
        assert off.reachable_target_distance(1.0) is None

    def test_an_unusable_atr_yields_no_ceiling_rather_than_a_guess(self) -> None:
        """Warm-up must not invent a distance. No judgement means no cap."""
        from xauusd.config.settings import Settings

        s = Settings()
        assert s.reachable_target_distance(float("nan")) is None
        assert s.reachable_target_distance(0.0) is None
