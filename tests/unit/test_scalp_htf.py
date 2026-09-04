"""The H4/H1/M15 read for the scalp tier.

Three properties matter here and each has a test that fails loudly if it stops holding:

1. Missing higher-timeframe data can never raise the factor. Degradation is
   one-directional everywhere else in this system and it has to be here too.
2. Higher-timeframe obstacles can only pull a target IN. A read that could widen a
   target would be a way to manufacture RR out of a chart opinion.
3. Only what is *ahead* of the entry is an obstacle. A level behind the entry cannot
   stop the trade, and letting one through would let `structural_target` drag a target
   backwards past the entry.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from xauusd.domain.enums import (
    Bias,
    Direction,
    FVGState,
    LevelKind,
    LiquidityKind,
    OrderBlockKind,
    Timeframe,
    ZoneState,
)
from xauusd.domain.types import (
    FVG,
    DealingRange,
    LiquidityPool,
    MarketSnapshot,
    OrderBlock,
    Quote,
    SRLevel,
    TimeframeStructure,
)
from xauusd.strategy.scalp.htf import ALIGNMENT_WEIGHTS, OBSTACLE_TFS, read_htf

TS = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
ATR = 1.0
ENTRY = 2000.0


def _structure(tf: Timeframe, bias: Bias) -> TimeframeStructure:
    return TimeframeStructure(
        timeframe=tf, bias=bias, last_event=None, swings=(), dealing_range=None
    )


@pytest.fixture(scope="module")
def base() -> MarketSnapshot:
    """A real snapshot from the synthetic fixture, used as a chassis.

    Built rather than hand-constructed: SessionState, VolatilityState and the rest have
    invariants of their own, and a hand-rolled stand-in would be a second definition of
    a snapshot free to drift from the one the engine actually sees.
    """
    from datetime import timedelta

    from tests.fixtures.synthetic import market_m1
    from xauusd.config.settings import Settings
    from xauusd.core.analyzer import MarketAnalyzer
    from xauusd.data.marketview import InMemoryBarSource, MarketView

    data = market_m1(12_000, seed=4)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]
    bar = m1.bar_at(len(m1) - 2)
    now = bar.ts + timedelta(seconds=60)
    view = MarketView(source, "GOLD", now, Quote(now, bar.close - 0.05, bar.close + 0.05))
    return MarketAnalyzer(Settings()).analyze(view, None, None, 25.0, 25.0)


def _snapshot(
    base: MarketSnapshot,
    *,
    biases: dict[Timeframe, Bias] | None = None,
    fvgs: tuple[FVG, ...] = (),
    order_blocks: tuple[OrderBlock, ...] = (),
    sr_levels: tuple[SRLevel, ...] = (),
    liquidity: tuple[LiquidityPool, ...] = (),
    dealing_range: DealingRange | None = None,
) -> MarketSnapshot:
    """The chassis with every field the HTF read touches replaced by a known one."""
    return replace(
        base,
        ts=TS,
        structures={tf: _structure(tf, b) for tf, b in (biases or {}).items()},
        liquidity=liquidity,
        sweeps=(),
        fvgs=fvgs,
        order_blocks=order_blocks,
        sr_levels=sr_levels,
        dealing_range=dealing_range,
    )


def _fvg(tf: Timeframe, direction: Direction, top: float, bottom: float) -> FVG:
    return FVG(
        timeframe=tf,
        direction=direction,
        formed_ts=TS,
        top=top,
        bottom=bottom,
        size=top - bottom,
        size_atr=(top - bottom) / ATR,
        displacement_atr=1.0,
        state=FVGState.UNMITIGATED,
    )


def _ob(tf: Timeframe, direction: Direction, top: float, bottom: float) -> OrderBlock:
    return OrderBlock(
        kind=OrderBlockKind.BULL_OB if direction is Direction.LONG else OrderBlockKind.BEAR_OB,
        timeframe=tf,
        direction=direction,
        formed_ts=TS,
        top=top,
        bottom=bottom,
        open_price=bottom,
        close_price=top,
        state=ZoneState.FRESH,
    )


def _level(tf: Timeframe, price: float, kind: LevelKind = LevelKind.SUPPORT) -> SRLevel:
    return SRLevel(
        kind=kind,
        timeframe=tf,
        price=price,
        band_upper=price + 0.2,
        band_lower=price - 0.2,
        formed_ts=TS,
    )


# --------------------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------------------


class TestAlignmentReadsTheTimeframesThatBoundTheHold:
    def test_m15_h1_h4_are_all_weighted(self, base) -> None:
        """The three timeframes the brief named must all reach the factor."""
        weighted = {tf for tf, _ in ALIGNMENT_WEIGHTS}
        assert Timeframe.M15 in weighted
        assert Timeframe.H1 in weighted
        assert Timeframe.H4 in weighted

    def test_the_fast_timeframes_outweigh_the_daily(self, base) -> None:
        """A ninety-minute trade is not governed by a bar that outlives it.

        The previous implementation gave D1 the largest share of the three it read,
        which meant a daily bias could suppress a signal whose whole life fitted inside
        one of its candles.
        """
        w = dict(ALIGNMENT_WEIGHTS)
        assert w[Timeframe.M15] > w[Timeframe.D1]
        assert w[Timeframe.H1] > w[Timeframe.D1]
        assert w[Timeframe.M15] + w[Timeframe.H1] > w[Timeframe.H4] + w[Timeframe.D1]

    def test_full_agreement_scores_one(self, base) -> None:
        snap = _snapshot(base, biases={tf: Bias.BULLISH for tf, _ in ALIGNMENT_WEIGHTS})
        ctx = read_htf(snap, Direction.LONG, ENTRY, ATR)
        assert ctx.alignment == pytest.approx(1.0)
        assert set(ctx.aligned_with) == {str(tf) for tf, _ in ALIGNMENT_WEIGHTS}

    def test_full_disagreement_scores_zero(self, base) -> None:
        snap = _snapshot(base, biases={tf: Bias.BEARISH for tf, _ in ALIGNMENT_WEIGHTS})
        ctx = read_htf(snap, Direction.LONG, ENTRY, ATR)
        assert ctx.alignment == pytest.approx(0.0)
        assert len(ctx.conflicts_with) == len(ALIGNMENT_WEIGHTS)

    def test_neutral_scores_half(self, base) -> None:
        snap = _snapshot(base, biases={tf: Bias.NEUTRAL for tf, _ in ALIGNMENT_WEIGHTS})
        ctx = read_htf(snap, Direction.LONG, ENTRY, ATR)
        assert ctx.alignment == pytest.approx(0.5)


class TestMissingDataNeverHelps:
    def test_absent_structures_score_below_neutral(self, base) -> None:
        """Unknown is not neutral. A dead analyzer must not read as a calm market."""
        missing = read_htf(_snapshot(base, biases={}), Direction.LONG, ENTRY, ATR)
        neutral = read_htf(
            _snapshot(base, biases={tf: Bias.NEUTRAL for tf, _ in ALIGNMENT_WEIGHTS}),
            Direction.LONG,
            ENTRY,
            ATR,
        )
        assert missing.alignment < neutral.alignment
        assert set(missing.missing) == {str(tf) for tf, _ in ALIGNMENT_WEIGHTS}

    def test_dropping_a_timeframe_can_never_raise_the_factor(self, base) -> None:
        """Property test over every subset boundary: removal is monotone downward.

        This is the invariant that stops a data outage from unlocking trades. It has to
        hold for an agreeing timeframe, which is the case where losing it is tempting to
        treat as harmless.
        """
        full = {tf: Bias.BULLISH for tf, _ in ALIGNMENT_WEIGHTS}
        complete = read_htf(_snapshot(base, biases=full), Direction.LONG, ENTRY, ATR)
        for tf, _ in ALIGNMENT_WEIGHTS:
            reduced = {k: v for k, v in full.items() if k is not tf}
            partial = read_htf(_snapshot(base, biases=reduced), Direction.LONG, ENTRY, ATR)
            assert partial.alignment <= complete.alignment, f"losing {tf} raised the factor"

    def test_no_atr_means_no_confluence_rather_than_full_confluence(self, base) -> None:
        snap = _snapshot(base, fvgs=(_fvg(Timeframe.H1, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5),))
        assert read_htf(snap, Direction.LONG, ENTRY, 0.0).confluence == 0.0


# --------------------------------------------------------------------------------------
# Confluence
# --------------------------------------------------------------------------------------


class TestConfluenceLooksAtWhereTheEntryActuallyIs:
    def test_an_agreeing_htf_fvg_at_the_entry_counts(self, base) -> None:
        snap = _snapshot(base, fvgs=(_fvg(Timeframe.H1, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5),))
        ctx = read_htf(snap, Direction.LONG, ENTRY, ATR)
        assert ctx.confluence > 0
        assert any("H1" in n for n in ctx.confluence_notes)

    def test_an_opposing_htf_fvg_does_not(self, base) -> None:
        snap = _snapshot(
            base, fvgs=(_fvg(Timeframe.H1, Direction.SHORT, ENTRY + 0.5, ENTRY - 0.5),)
        )
        assert read_htf(snap, Direction.LONG, ENTRY, ATR).confluence == 0.0

    def test_a_distant_htf_zone_does_not(self, base) -> None:
        """Ten ATR away is not where the entry is; it is merely on the same chart."""
        far = _fvg(Timeframe.H4, Direction.LONG, ENTRY - 9.0, ENTRY - 10.0)
        assert read_htf(_snapshot(base, fvgs=(far,)), Direction.LONG, ENTRY, ATR).confluence == 0.0

    def test_an_m5_zone_is_not_htf_confluence(self, base) -> None:
        """M5 is the scalp's own structure timeframe; counting it would double-count."""
        m5 = _fvg(Timeframe.M5, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5)
        assert read_htf(_snapshot(base, fvgs=(m5,)), Direction.LONG, ENTRY, ATR).confluence == 0.0

    def test_support_under_a_long_counts_and_resistance_over_it_does_not(self, base) -> None:
        under = _snapshot(base, sr_levels=(_level(Timeframe.H4, ENTRY - 0.1),))
        over = _snapshot(base, sr_levels=(_level(Timeframe.H4, ENTRY + 0.1, LevelKind.RESISTANCE),))
        assert read_htf(under, Direction.LONG, ENTRY, ATR).confluence > 0
        assert read_htf(over, Direction.LONG, ENTRY, ATR).confluence == 0.0

    def test_discount_helps_a_long_and_premium_does_not(self, base) -> None:
        low = DealingRange(
            high=ENTRY + 80, low=ENTRY - 20, high_ts=TS, low_ts=TS, timeframe=Timeframe.H4
        )
        high = DealingRange(
            high=ENTRY + 20, low=ENTRY - 80, high_ts=TS, low_ts=TS, timeframe=Timeframe.H4
        )
        assert (
            read_htf(_snapshot(base, dealing_range=low), Direction.LONG, ENTRY, ATR).confluence > 0
        )
        assert (
            read_htf(_snapshot(base, dealing_range=high), Direction.LONG, ENTRY, ATR).confluence
            == 0.0
        )

    def test_more_sources_score_higher_than_one(self, base) -> None:
        one = _snapshot(base, fvgs=(_fvg(Timeframe.H1, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5),))
        three = _snapshot(
            base,
            fvgs=(_fvg(Timeframe.H1, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5),),
            order_blocks=(_ob(Timeframe.H4, Direction.LONG, ENTRY + 0.3, ENTRY - 0.3),),
            sr_levels=(_level(Timeframe.M15, ENTRY - 0.1),),
        )
        assert (
            read_htf(three, Direction.LONG, ENTRY, ATR).confluence
            > read_htf(one, Direction.LONG, ENTRY, ATR).confluence
        )


# --------------------------------------------------------------------------------------
# Obstacles — the part that changes trades rather than scores
# --------------------------------------------------------------------------------------


class TestObstaclesAreOnlyWhatIsAhead:
    def test_a_level_above_a_long_is_an_obstacle(self, base) -> None:
        snap = _snapshot(base, sr_levels=(_level(Timeframe.H4, ENTRY + 5.0, LevelKind.RESISTANCE),))
        assert read_htf(snap, Direction.LONG, ENTRY, ATR).obstacles

    def test_a_level_below_a_long_is_not(self, base) -> None:
        """A level behind the entry cannot stop the trade reaching its target.

        Including it would let `structural_target` pull the target backwards through
        the entry, producing a negative-RR "trade".
        """
        snap = _snapshot(base, sr_levels=(_level(Timeframe.H4, ENTRY - 5.0),))
        assert read_htf(snap, Direction.LONG, ENTRY, ATR).obstacles == ()

    def test_the_mirror_holds_for_a_short(self, base) -> None:
        below = _snapshot(base, sr_levels=(_level(Timeframe.H4, ENTRY - 5.0),))
        above = _snapshot(
            base, sr_levels=(_level(Timeframe.H4, ENTRY + 5.0, LevelKind.RESISTANCE),)
        )
        assert read_htf(below, Direction.SHORT, ENTRY, ATR).obstacles
        assert read_htf(above, Direction.SHORT, ENTRY, ATR).obstacles == ()

    def test_resting_htf_liquidity_ahead_is_an_obstacle(self, base) -> None:
        pool = LiquidityPool(
            kind=LiquidityKind.EQH,
            timeframe=Timeframe.H1,
            price=ENTRY + 4.0,
            formed_ts=TS,
        )
        assert read_htf(_snapshot(base, liquidity=(pool,)), Direction.LONG, ENTRY, ATR).obstacles

    def test_swept_liquidity_is_not(self, base) -> None:
        pool = LiquidityPool(
            kind=LiquidityKind.EQH,
            timeframe=Timeframe.H1,
            price=ENTRY + 4.0,
            formed_ts=TS,
            swept_ts=TS,
        )
        assert (
            read_htf(_snapshot(base, liquidity=(pool,)), Direction.LONG, ENTRY, ATR).obstacles == ()
        )

    def test_an_opposing_zone_ahead_is_an_obstacle_and_an_agreeing_one_is_not(self, base) -> None:
        against = _snapshot(
            base, fvgs=(_fvg(Timeframe.H4, Direction.SHORT, ENTRY + 6.0, ENTRY + 5.0),)
        )
        withit = _snapshot(
            base, fvgs=(_fvg(Timeframe.H4, Direction.LONG, ENTRY + 6.0, ENTRY + 5.0),)
        )
        assert read_htf(against, Direction.LONG, ENTRY, ATR).obstacles
        assert read_htf(withit, Direction.LONG, ENTRY, ATR).obstacles == ()

    def test_only_htf_timeframes_supply_obstacles(self, base) -> None:
        """M1/M5 obstacles come from MicroSnapshot; duplicating them here would be two
        sources of the same fact, free to disagree."""
        assert Timeframe.M5 not in OBSTACLE_TFS
        assert Timeframe.M1 not in OBSTACLE_TFS
        snap = _snapshot(base, sr_levels=(_level(Timeframe.M5, ENTRY + 5.0, LevelKind.RESISTANCE),))
        assert read_htf(snap, Direction.LONG, ENTRY, ATR).obstacles == ()


class TestObstaclesCanOnlyShrinkATarget:
    def test_adding_htf_obstacles_never_moves_a_target_further_out(self, base) -> None:
        """The safety property. `structural_target` picks the nearest obstacle inside
        the band, so a longer obstacle list can only ever produce a nearer target — but
        that is a claim about the function, so it is asserted rather than assumed."""
        from xauusd.strategy.scalp.base import structural_target

        entry, stop = 2000.0, 1998.0
        base_obstacles = [2010.0]
        plain, _ = structural_target(entry, stop, Direction.LONG, 3.0, base_obstacles)
        for extra in ([2004.0], [2002.5], [2001.6], [1990.0], [2050.0]):
            withhtf, _ = structural_target(entry, stop, Direction.LONG, 3.0, base_obstacles + extra)
            assert withhtf <= plain, f"{extra} pushed the target out"

    def test_the_same_holds_for_a_short(self, base) -> None:
        from xauusd.strategy.scalp.base import structural_target

        entry, stop = 2000.0, 2002.0
        base_obstacles = [1990.0]
        plain, _ = structural_target(entry, stop, Direction.SHORT, 3.0, base_obstacles)
        for extra in ([1996.0], [1997.5], [1998.4], [2010.0]):
            withhtf, _ = structural_target(
                entry, stop, Direction.SHORT, 3.0, base_obstacles + extra
            )
            assert withhtf >= plain, f"{extra} pushed the target out"


class TestTheFactorIsBounded:
    def test_between_zero_and_one_at_the_extremes(self, base) -> None:
        best = _snapshot(
            base,
            biases={tf: Bias.BULLISH for tf, _ in ALIGNMENT_WEIGHTS},
            fvgs=(_fvg(Timeframe.H1, Direction.LONG, ENTRY + 0.5, ENTRY - 0.5),),
            order_blocks=(_ob(Timeframe.H4, Direction.LONG, ENTRY + 0.3, ENTRY - 0.3),),
            sr_levels=(_level(Timeframe.M15, ENTRY - 0.1),),
            dealing_range=DealingRange(
                high=ENTRY + 80, low=ENTRY - 20, high_ts=TS, low_ts=TS, timeframe=Timeframe.H4
            ),
        )
        worst = _snapshot(base, biases={tf: Bias.BEARISH for tf, _ in ALIGNMENT_WEIGHTS})
        assert read_htf(best, Direction.LONG, ENTRY, ATR).factor == pytest.approx(1.0)
        assert read_htf(worst, Direction.LONG, ENTRY, ATR).factor == pytest.approx(0.0)

    def test_alignment_alone_can_still_score_well(self, base) -> None:
        """An entry with no HTF feature near it is not disqualified by that alone."""
        snap = _snapshot(base, biases={tf: Bias.BULLISH for tf, _ in ALIGNMENT_WEIGHTS})
        assert read_htf(snap, Direction.LONG, ENTRY, ATR).factor >= 0.6
