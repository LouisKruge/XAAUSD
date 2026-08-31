"""MarketView: the look-ahead boundary. If these tests pass, backtests mean something."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.synthetic import trend
from xauusd.core.indicators import atr, ema, percentile_rank, rsi, sma
from xauusd.data.marketview import InMemoryBarSource, LookAheadError, MarketView
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar

UTC = UTC
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


def source(n: int = 20, tf: Timeframe = Timeframe.M5) -> InMemoryBarSource:
    src = InMemoryBarSource()
    src.set_bars(
        tf,
        [
            Bar(T0 + timedelta(seconds=tf.seconds * i), 2000 + i, 2005 + i, 1995 + i, 2002 + i)
            for i in range(n)
        ],
    )
    return src


class TestLookAheadPrevention:
    def test_forming_bar_is_never_visible(self) -> None:
        """The bar currently forming has an unknown high/low. It must not be returned."""
        v = MarketView(source(), "XAUUSD", T0 + timedelta(minutes=32))
        bars = v.bars(Timeframe.M5)
        # 08:30 bar is forming at 08:32; the last CLOSED bar opened 08:25.
        assert bars.last_ts == T0 + timedelta(minutes=25)

    def test_bar_becomes_visible_exactly_at_its_close(self) -> None:
        just_before = MarketView(source(), "XAUUSD", T0 + timedelta(minutes=29, seconds=59))
        exactly_at = MarketView(source(), "XAUUSD", T0 + timedelta(minutes=30))
        assert just_before.bar_count(Timeframe.M5) == 5
        assert exactly_at.bar_count(Timeframe.M5) == 6

    def test_no_bar_ever_returned_is_in_the_future(self) -> None:
        src = source(50)
        for minutes in range(0, 250, 7):
            now = T0 + timedelta(minutes=minutes)
            v = MarketView(src, "XAUUSD", now)
            for tf in (Timeframe.M5,):
                bars = v.bars(tf)
                for i in range(len(bars)):
                    assert bars.close_time(i) <= now

    @settings(max_examples=100, deadline=None)
    @given(offset_minutes=st.integers(min_value=0, max_value=600))
    def test_property_no_future_data_at_any_instant(self, offset_minutes: int) -> None:
        src = source(120)
        now = T0 + timedelta(minutes=offset_minutes)
        v = MarketView(src, "XAUUSD", now)
        bars = v.bars(Timeframe.M5)
        if len(bars):
            assert bars.close_time(len(bars) - 1) <= now

    def test_future_bars_raises_with_an_explanation(self) -> None:
        v = MarketView(source(), "XAUUSD", T0 + timedelta(hours=1))
        with pytest.raises(LookAheadError, match="cannot see the future"):
            v.future_bars(Timeframe.M5, 5)

    def test_assert_not_future_guards_external_timestamps(self) -> None:
        v = MarketView(source(), "XAUUSD", T0 + timedelta(hours=1))
        v.assert_not_future(T0, "calendar event")  # past is fine
        with pytest.raises(LookAheadError):
            v.assert_not_future(T0 + timedelta(hours=2), "calendar event")

    def test_bars_between_clamps_end_to_now(self) -> None:
        v = MarketView(source(50), "XAUUSD", T0 + timedelta(minutes=60))
        got = v.bars_between(Timeframe.M5, T0, T0 + timedelta(days=1))
        assert got.close_time(len(got) - 1) <= v.now

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            MarketView(source(), "XAUUSD", datetime(2026, 1, 5, 8, 0))

    def test_view_advances_without_mutation(self) -> None:
        v1 = MarketView(source(50), "XAUUSD", T0 + timedelta(minutes=30))
        v2 = v1.at(T0 + timedelta(minutes=60))
        assert v1.bar_count(Timeframe.M5) == 6
        assert v2.bar_count(Timeframe.M5) == 12
        assert v1.now < v2.now


class TestIndicatorCausality:
    """An indicator that changes its past when new data arrives is repainting."""

    @pytest.mark.parametrize(
        "fn",
        [
            lambda v: sma(v, 20),
            lambda v: ema(v, 20),
            lambda v: rsi(v, 14),
            lambda v: percentile_rank(v, 50),
        ],
    )
    def test_truncating_input_does_not_change_earlier_output(self, fn) -> None:
        vals = np.cumsum(np.random.RandomState(0).randn(300)) + 2000
        full = fn(vals)
        for cut in (100, 200, 250):
            assert np.allclose(full[:cut], fn(vals[:cut]), equal_nan=True), f"repaints at {cut}"

    def test_atr_is_causal(self) -> None:
        s = trend(300)
        full = atr(s, 14)
        part = atr(s.slice(0, 200), 14)
        assert np.allclose(full[:200], part, equal_nan=True)

    def test_insufficient_history_returns_nan_not_a_guess(self) -> None:
        vals = np.arange(5.0)
        assert np.all(np.isnan(sma(vals, 20)))
        assert np.all(np.isnan(rsi(vals, 14)))

    def test_known_vectors(self) -> None:
        assert sma(np.arange(1.0, 11.0), 4)[-1] == pytest.approx(8.5)
        assert rsi(np.arange(1.0, 40.0))[-1] == pytest.approx(100.0)
        assert rsi(np.arange(40.0, 1.0, -1.0))[-1] == pytest.approx(0.0)


class TestBarSeries:
    def test_rejects_out_of_order_bars(self) -> None:
        bars = [Bar(T0 + timedelta(minutes=5), 1, 1, 1, 1), Bar(T0, 1, 1, 1, 1)]
        with pytest.raises(ValueError, match="strictly increasing"):
            BarSeries.from_bars(Timeframe.M5, bars)

    def test_empty_series_is_falsy_and_safe(self) -> None:
        s = BarSeries.empty(Timeframe.M5)
        assert not s and len(s) == 0
        with pytest.raises(IndexError):
            _ = s.last

    def test_round_trip(self) -> None:
        src = source(10)
        s = src.series("XAUUSD", Timeframe.M5)
        assert [b.ts for b in s.to_bars()] == [b.ts for b in s]
        assert s.bar_at(0).open == 2000
