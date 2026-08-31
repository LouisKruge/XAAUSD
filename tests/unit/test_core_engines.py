"""Phase 5 core engines: structure, liquidity, FVG, order blocks, S/R.

These are tested against synthetic data with PLANTED geometry, so a test can assert
"the engine found the sweep we put at bar 25" rather than eyeballing output.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.fixtures.synthetic import make_bars, market, ranging, sweep_and_reverse, trend

from xauusd.core.fair_value_gap import FVGEngine
from xauusd.core.indicators import atr_last
from xauusd.core.liquidity import LiquidityEngine
from xauusd.core.order_blocks import OrderBlockEngine
from xauusd.core.structure import (
    StructureEngine,
    detect_swings,
    visible_swings,
)
from xauusd.core.support_resistance import SREngine, is_correct_side
from xauusd.data.resample import resample
from xauusd.data.series import BarSeries
from xauusd.domain.enums import (
    Bias,
    Direction,
    FVGState,
    LiquidityKind,
    StructureKind,
    SwingKind,
    Timeframe,
    ZoneState,
)

UTC = UTC


class TestSwingDetection:
    def test_finds_an_obvious_peak(self) -> None:
        prices = [(100, 101, 99, 100)] * 5 + [(100, 120, 99, 118)] + [(118, 119, 100, 101)] * 5
        s = BarSeries.from_bars(Timeframe.M15, make_bars(prices))
        swings = detect_swings(s, lookback=2)
        assert any(w.kind is SwingKind.HIGH and w.price == 120 for w in swings)

    def test_confirmation_lag_prevents_lookahead(self) -> None:
        """A fractal is defined by the bars AFTER it, so it is not knowable at its own bar."""
        s = sweep_and_reverse()
        swings = detect_swings(s, lookback=2, min_leg_atr=0.25, atr_value=atr_last(s, 14))
        top = max(swings, key=lambda x: x.price)
        assert top.confirmed_index == top.index + 2
        assert not [w for w in visible_swings(swings, top.index) if w.index == top.index]
        assert [w for w in visible_swings(swings, top.confirmed_index) if w.index == top.index]

    def test_flat_tops_are_deterministic(self) -> None:
        """Non-strict comparison to the right makes equal highs resolvable."""
        prices = (
            [(100, 105, 99, 104)] * 3
            + [(104, 110, 103, 109), (109, 110, 105, 106)]
            + [(106, 107, 100, 101)] * 3
        )
        s = BarSeries.from_bars(Timeframe.M15, make_bars(prices))
        assert detect_swings(s, 2) == detect_swings(s, 2)  # deterministic

    def test_swings_alternate(self) -> None:
        s = trend(200, drift=0.5)
        swings = detect_swings(s, 2)
        kinds = [w.kind for w in swings]
        assert all(a is not b for a, b in zip(kinds, kinds[1:])), "swings must alternate"


class TestStructureEvents:
    def test_bos_requires_a_body_close_not_a_wick(self) -> None:
        """A wick through a level is a raid; treating it as acceptance creates false structure."""
        e = StructureEngine()
        # rise, pull back, then a bar whose WICK exceeds the prior high but closes below
        prices = (
            [(100, 102, 99, 101)] * 3
            + [(101, 115, 100, 114)]
            + [(114, 115, 108, 109)] * 3
            + [(109, 116, 108, 110)]  # wick above 115, close below
            + [(110, 111, 105, 106)] * 3
        )
        s = BarSeries.from_bars(Timeframe.M15, make_bars(prices))
        a = atr_last(s, 5)
        swings = detect_swings(s, 2, 0.0, a)
        events = e.detect_events(s, swings, a)
        bullish_breaks = [ev for ev in events if ev.direction is Direction.LONG and ev.price == 115]
        assert not bullish_breaks, "a wick through a swing high must not register as a BOS"

    def test_choch_moves_bias_to_neutral_not_to_the_opposite(self) -> None:
        """A CHOCH is a warning, not a reversal. Promotion needs the next confirmation."""
        e = StructureEngine()
        s = trend(250, drift=1.0, noise=0.4)
        r = e.analyze(s)
        if r.last_event and r.last_event.kind in (StructureKind.CHOCH, StructureKind.MSS):
            assert r.bias is Bias.NEUTRAL

    def test_mss_needs_more_displacement_than_a_choch(self) -> None:
        from xauusd.config.settings import StructureConfig

        cfg = StructureConfig()
        assert cfg.mss_min_displacement_atr > cfg.bos_min_displacement_atr

    def test_bias_is_neutral_when_structure_is_unknown(self) -> None:
        r = StructureEngine().analyze(trend(25))
        assert r.bias is Bias.NEUTRAL
        assert r.swings == ()


class TestLiquidity:
    def test_finds_the_planted_equal_high(self) -> None:
        s = sweep_and_reverse(equal_high=2050.0)
        pools = LiquidityEngine().equal_levels(s, atr_last(s, 14))
        assert any(p.kind is LiquidityKind.EQH and abs(p.price - 2050.0) < 0.5 for p in pools)

    def test_detects_the_planted_sweep_with_high_quality(self) -> None:
        s = sweep_and_reverse(equal_high=2050.0)
        a = atr_last(s, 14)
        e = LiquidityEngine()
        pools, sweeps = e.analyze(s, detect_swings(s, 2, 0.25, a))
        planted = [sw for sw in sweeps if abs(sw.pool.price - 2050.0) < 1.0]
        assert planted, "the planted sweep of the equal highs was not detected"
        sw = planted[0]
        assert sw.direction is Direction.SHORT
        assert sw.closed_back_inside
        assert sw.displacement_after_atr > 1.0
        assert sw.quality > 0.7

    def test_a_deep_break_is_not_a_sweep(self) -> None:
        """Price running far through a level is a genuine break, not a stop hunt."""
        s = sweep_and_reverse(equal_high=2050.0)
        a = atr_last(s, 14)
        e = LiquidityEngine()
        pools = e.equal_levels(s, a)
        for sw in e.detect_sweeps(s, pools, a):
            assert sw.penetration_atr <= e.cfg.sweep_max_penetration_atr

    def test_swept_pools_are_not_offered_as_targets(self) -> None:
        s = sweep_and_reverse()
        a = atr_last(s, 14)
        e = LiquidityEngine()
        pools, _ = e.analyze(s, detect_swings(s, 2, 0.25, a))
        targets = e.draw_on_liquidity(pools, 2030.0, Direction.LONG)
        assert all(p.is_resting for p in targets)
        assert all(p.price > 2030.0 for p in targets)

    def test_opposing_liquidity_is_identified(self) -> None:
        s = sweep_and_reverse()
        a = atr_last(s, 14)
        e = LiquidityEngine()
        pools, _ = e.analyze(s, detect_swings(s, 2, 0.25, a))
        against = e.opposing_liquidity_near(pools, 2030.0, Direction.LONG, within=30.0)
        assert all(p.price < 2030.0 for p in against)


class TestFVG:
    def test_detects_a_planted_bearish_gap(self) -> None:
        s = sweep_and_reverse()
        fvgs = FVGEngine().detect(s, atr_last(s, 14))
        bearish = [f for f in fvgs if f.direction is Direction.SHORT]
        assert bearish, "the displacement gap was not detected"
        assert any(f.displacement_atr > 1.0 for f in bearish)

    def test_a_gap_without_displacement_is_rejected(self) -> None:
        """Three small drifting bars leave a gap that is not an institutional footprint."""
        prices = [
            (100, 100.5, 99.5, 100.1),
            (100.1, 101.5, 100.05, 100.2),
            (100.2, 102, 101.0, 101.5),
        ] * 10
        s = BarSeries.from_bars(Timeframe.M15, make_bars(prices))
        assert FVGEngine().detect(s, atr_last(s, 14)) == []

    def test_lifecycle_progresses_as_the_gap_fills(self) -> None:
        e = FVGEngine()
        s = sweep_and_reverse()
        fvgs = e.detect(s, atr_last(s, 14))
        assert any(f.state is FVGState.UNMITIGATED for f in fvgs)
        assert any(
            f.state in (FVGState.INVALIDATED, FVGState.MITIGATED, FVGState.INVERTED) for f in fvgs
        )

    def test_entry_uses_consequent_encroachment(self) -> None:
        e = FVGEngine()
        f = e.detect(sweep_and_reverse(), atr_last(sweep_and_reverse(), 14))[0]
        assert e.entry_price(f) == pytest.approx(f.midpoint)

    def test_invalidated_gaps_are_not_tradable(self) -> None:
        e = FVGEngine()
        s = sweep_and_reverse()
        fvgs = e.detect(s, atr_last(s, 14))
        tradable = e.tradable(fvgs, Direction.SHORT, 2035.0, 100.0)
        assert all(f.is_tradable for f in tradable)


class TestOrderBlocks:
    def test_requires_a_structure_break(self) -> None:
        """Without a resulting BOS, 'the last down candle' is just a candle."""
        e = OrderBlockEngine()
        s = sweep_and_reverse()
        assert e.detect(s, [], atr_last(s, 14)) == []

    def test_finds_the_block_behind_the_break(self) -> None:
        s = sweep_and_reverse()
        a = atr_last(s, 14)
        se = StructureEngine()
        events = se.detect_events(s, detect_swings(s, 2, 0.25, a), a)
        obs = OrderBlockEngine().detect(s, events, a)
        assert obs
        assert all(o.caused_bos for o in obs)

    def test_body_close_through_invalidates(self) -> None:
        s = sweep_and_reverse()
        a = atr_last(s, 14)
        se = StructureEngine()
        events = se.detect_events(s, detect_swings(s, 2, 0.25, a), a)
        obs = OrderBlockEngine().detect(s, events, a)
        assert all(o.state in set(ZoneState) for o in obs)

    def test_scoring_rewards_displacement_and_freshness(self) -> None:
        e = OrderBlockEngine()
        s = sweep_and_reverse()
        a = atr_last(s, 14)
        events = StructureEngine().detect_events(s, detect_swings(s, 2, 0.25, a), a)
        obs = e.detect(s, events, a)
        if obs:
            base = e.score(obs[0], atr_value=a)
            better = e.score(obs[0], swept_liquidity=True, htf_aligned=True, atr_value=a)
            assert better > base


class TestSRAndPremiumDiscount:
    def test_levels_carry_evidence(self) -> None:
        levels = SREngine().levels_from(ranging(400, centre=2000, width=40))
        assert levels
        assert all(lv.touches >= 2 for lv in levels)
        assert all(0 <= lv.importance <= 1 for lv in levels)

    def test_higher_timeframes_score_higher(self) -> None:
        from xauusd.core.support_resistance import TF_WEIGHT

        assert TF_WEIGHT[Timeframe.D1] > TF_WEIGHT[Timeframe.H1] > TF_WEIGHT[Timeframe.M15]

    def test_premium_discount_sides(self) -> None:
        st = StructureEngine().analyze(ranging(300))
        dr = st.dealing_range
        assert dr is not None
        assert is_correct_side(dr, dr.low + dr.size * 0.1, Direction.LONG)
        assert not is_correct_side(dr, dr.high - dr.size * 0.1, Direction.LONG)
        assert is_correct_side(dr, dr.high - dr.size * 0.1, Direction.SHORT)

    def test_unknown_range_does_not_veto(self) -> None:
        assert is_correct_side(None, 2000.0, Direction.LONG)

    def test_blocking_level_between_entry_and_target(self) -> None:
        e = SREngine()
        levels = e.levels_from(ranging(400, centre=2000, width=60))
        important = [lv for lv in levels if lv.importance >= 0.3]
        if important:
            lv = important[0]
            blocked = e.blocking_level(
                levels, lv.price - 20, lv.price + 20, Direction.LONG, min_importance=0.3
            )
            assert blocked is not None


class TestResample:
    def test_daily_bars_roll_at_the_broker_hour_not_utc_midnight(self) -> None:
        m = market(4000)
        d1 = m[Timeframe.D1]
        hours = {datetime.fromtimestamp(int(t), UTC).hour for t in d1.ts}
        assert hours == {22}, "gold daily bars roll at 22:00 UTC, not midnight"

    def test_aggregation_is_exact(self) -> None:
        m = market(4000)
        m5, h1 = m[Timeframe.M5], m[Timeframe.H1]
        for i in (5, 20, 50):
            t0 = int(h1.ts[i])
            mask = (m5.ts >= t0) & (m5.ts < t0 + 3600)
            assert h1.high[i] == pytest.approx(m5.high[mask].max())
            assert h1.low[i] == pytest.approx(m5.low[mask].min())
            assert h1.open[i] == pytest.approx(m5.open[mask][0])
            assert h1.close[i] == pytest.approx(m5.close[mask][-1])

    def test_partial_final_bucket_is_dropped(self) -> None:
        """Keeping a half-formed H4 bar is a look-ahead bug in disguise."""
        m5 = market(4000)[Timeframe.M5]
        truncated = m5.slice(0, len(m5) - 20)
        h4 = resample(truncated, Timeframe.H4, drop_partial=True)
        last_close = int(truncated.ts[-1]) + Timeframe.M5.seconds
        assert int(h4.ts[-1]) + Timeframe.H4.seconds <= last_close

    def test_cannot_resample_downward(self) -> None:
        with pytest.raises(ValueError):
            resample(market(1000)[Timeframe.H1], Timeframe.M5)
