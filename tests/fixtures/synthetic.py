"""Synthetic market generators for deterministic tests.

Real gold data would be better, but it cannot be committed and would make tests depend
on a download. These generators produce price paths with KNOWN structure, so a test can
assert "the engine found the sweep we planted at bar 40" rather than eyeballing output.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from xauusd.data.series import BarSeries
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar

UTC = UTC


def make_bars(
    prices: list[tuple[float, float, float, float]],
    tf: Timeframe = Timeframe.M15,
    start: datetime | None = None,
    spread_points: int = 25,
) -> list[Bar]:
    t0 = start or datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    return [
        Bar(
            t0 + timedelta(seconds=tf.seconds * i),
            o,
            h,
            low,
            c,
            tick_volume=100,
            spread_points=spread_points,
        )
        for i, (o, h, low, c) in enumerate(prices)
    ]


def trend(
    n: int = 200,
    start_price: float = 2000.0,
    drift: float = 0.5,
    noise: float = 1.0,
    seed: int = 7,
    tf: Timeframe = Timeframe.H1,
    bar_range: float = 3.0,
) -> BarSeries:
    """A clean trend with controllable drift. drift>0 bullish, <0 bearish, 0 range."""
    rng = np.random.RandomState(seed)
    closes = start_price + np.cumsum(drift + rng.randn(n) * noise)
    prices = []
    prev = start_price
    for c in closes:
        o = prev
        hi = max(o, c) + abs(rng.randn()) * bar_range * 0.4
        lo = min(o, c) - abs(rng.randn()) * bar_range * 0.4
        prices.append((o, hi, lo, float(c)))
        prev = float(c)
    return BarSeries.from_bars(tf, make_bars(prices, tf))


def ranging(
    n: int = 200,
    centre: float = 2000.0,
    width: float = 20.0,
    seed: int = 3,
    tf: Timeframe = Timeframe.H1,
) -> BarSeries:
    rng = np.random.RandomState(seed)
    x = np.linspace(0, 8 * np.pi, n)
    closes = centre + np.sin(x) * (width / 2) + rng.randn(n) * (width * 0.05)
    prices, prev = [], centre
    for c in closes:
        o = prev
        hi = max(o, c) + abs(rng.randn()) * 1.0
        lo = min(o, c) - abs(rng.randn()) * 1.0
        prices.append((o, hi, lo, float(c)))
        prev = float(c)
    return BarSeries.from_bars(tf, make_bars(prices, tf))


def sweep_and_reverse(
    tf: Timeframe = Timeframe.M15,
    equal_high: float = 2050.0,
    base: float = 2000.0,
    start: datetime | None = None,
) -> BarSeries:
    """A textbook bearish setup with KNOWN geometry, for asserting detection.

    Layout:
      bars  0-19  : rally into `equal_high`, forming two equal highs (buyside liquidity)
      bars 20-24  : consolidation just below
      bar     25  : SWEEP - wicks above equal_high, closes back below (stop hunt)
      bars 26-29  : bearish DISPLACEMENT down, leaving a bearish FVG
      bars 30-33  : retrace back into the FVG  <- the entry
      bars 34-44  : continuation lower
    """
    p: list[tuple[float, float, float, float]] = []
    # rally with two equal highs
    for i in range(10):
        o = base + i * 4
        p.append((o, o + 3, o - 1, o + 3))
    p.append((base + 43, equal_high, base + 41, equal_high - 3))  # touch 1
    for _ in range(4):
        p.append((base + 40, base + 44, base + 37, base + 41))
    p.append((base + 41, equal_high - 0.2, base + 39, equal_high - 4))  # touch 2 (equal high)
    for _ in range(4):
        p.append((base + 46, base + 48, base + 43, base + 45))
    # consolidation
    for _ in range(5):
        p.append((base + 45, base + 47, base + 43, base + 45))
    # the sweep: takes the equal highs, closes back inside
    p.append((base + 45, equal_high + 6.0, base + 44, base + 44.5))
    # bearish displacement leaving an FVG (bar N-1 low > bar N+1 high)
    p.append((base + 44, base + 45, base + 30, base + 31))
    p.append((base + 31, base + 32, base + 20, base + 21))
    p.append((base + 21, base + 24, base + 18, base + 23))
    p.append((base + 23, base + 26, base + 22, base + 25))
    # retrace into the FVG zone
    for _ in range(4):
        p.append((base + 25, base + 32, base + 24, base + 30))
    # continuation lower
    for i in range(11):
        o = base + 28 - i * 3
        p.append((o, o + 2, o - 4, o - 3))
    return BarSeries.from_bars(tf, make_bars(p, tf, start))


def with_gap(
    n: int = 100, gap_at: int = 50, gap_size: float = 15.0, tf: Timeframe = Timeframe.H1
) -> BarSeries:
    s = trend(n, tf=tf, drift=0.0, seed=11)
    bars = s.to_bars()
    for i in range(gap_at, len(bars)):
        b = bars[i]
        bars[i] = Bar(
            b.ts,
            b.open + gap_size,
            b.high + gap_size,
            b.low + gap_size,
            b.close + gap_size,
            b.tick_volume,
            spread_points=b.spread_points,
        )
    return BarSeries.from_bars(tf, bars)


def market(
    n_m5: int = 6000,
    start_price: float = 2400.0,
    drift_per_bar: float = 0.02,
    noise: float = 0.55,
    seed: int = 17,
    start: datetime | None = None,
) -> dict[Timeframe, BarSeries]:
    """A CONSISTENT multi-timeframe market, derived from one M5 path.

    Generating each timeframe independently (the obvious shortcut) produces an H4 bias
    that contradicts the M5 bars being traded, which makes every downstream number
    meaningless. So: build M5 once, aggregate upward.
    """
    from xauusd.data.resample import build_timeframes

    rng = np.random.RandomState(seed)
    t0 = start or datetime(2026, 1, 4, 22, 0, tzinfo=UTC)

    # Intraday seasonality: London/NY hours move more than the Asian session.
    steps = np.arange(n_m5)
    hour = ((t0.hour * 3600 + steps * 300) // 3600) % 24
    activity = np.where((hour >= 7) & (hour < 21), 1.0, 0.45)

    rets = drift_per_bar + rng.randn(n_m5) * noise * activity
    closes = start_price + np.cumsum(rets)

    bars = []
    prev = start_price
    for i, c in enumerate(closes):
        o = prev
        wick = abs(rng.randn()) * noise * activity[i] * 1.6
        hi = max(o, c) + wick
        lo = min(o, c) - abs(rng.randn()) * noise * activity[i] * 1.6
        ts = t0 + timedelta(minutes=5 * i)
        # Skip the weekend: gold does not trade Saturday.
        if ts.weekday() == 5 or (ts.weekday() == 6 and ts.hour < 22):
            prev = float(c)
            continue
        bars.append(
            Bar(
                ts,
                float(o),
                float(hi),
                float(lo),
                float(c),
                tick_volume=int(80 * activity[i] + rng.randint(0, 40)),
                spread_points=int(20 + (1 - activity[i]) * 25),
            )
        )
        prev = float(c)

    m5 = BarSeries.from_bars(Timeframe.M5, bars)
    return build_timeframes(
        m5, [Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1]
    )
