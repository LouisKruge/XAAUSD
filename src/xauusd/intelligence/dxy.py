"""Dollar index.

Preference order (see docs/architecture/04-data-sources.md):
  1. the broker's own DXY/USDX CFD, if offered
  2. a SYNTHETIC index computed from MT5 FX majors  <- the default

The synthetic index is preferred over a third-party DXY feed because it is on the same
clock and the same connection as the gold data, is available from every broker, and is
backtestable over exactly the same history. A DXY series from a different vendor
introduces a timing basis you cannot see.

ICE US Dollar Index formula:
    DXY = 50.14348112 x EURUSD^-0.576 x USDJPY^0.136 x GBPUSD^-0.119
                      x USDCAD^0.091  x USDSEK^0.042 x USDCHF^0.036
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from xauusd.core.indicators import slope
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Bias

ICE_CONSTANT = 50.14348112
ICE_WEIGHTS: dict[str, float] = {
    "EURUSD": -0.576,
    "USDJPY": 0.136,
    "GBPUSD": -0.119,
    "USDCAD": 0.091,
    "USDSEK": 0.042,
    "USDCHF": 0.036,
}


class InsufficientDxyData(RuntimeError):
    pass


def synthetic_dxy(closes: dict[str, np.ndarray]) -> np.ndarray:
    """Compute the index from aligned close arrays. All six pairs are required."""
    missing = [p for p in ICE_WEIGHTS if p not in closes]
    if missing:
        raise InsufficientDxyData(f"missing pairs for synthetic DXY: {missing}")
    lengths = {len(closes[p]) for p in ICE_WEIGHTS}
    if len(lengths) != 1:
        raise InsufficientDxyData(f"pair series have different lengths: {lengths}")
    n = lengths.pop()
    if n == 0:
        raise InsufficientDxyData("empty series")

    out = np.full(n, ICE_CONSTANT, dtype=np.float64)
    for pair, weight in ICE_WEIGHTS.items():
        arr = np.asarray(closes[pair], dtype=np.float64)
        if np.any(arr <= 0):
            raise InsufficientDxyData(f"{pair} contains non-positive prices")
        out = out * np.power(arr, weight)
    return out


def synthetic_dxy_from_series(series: dict[str, BarSeries]) -> np.ndarray:
    """Align six pair series on their common timestamps, then compute the index."""
    missing = [p for p in ICE_WEIGHTS if p not in series or not len(series[p])]
    if missing:
        raise InsufficientDxyData(f"missing or empty pairs: {missing}")
    common = None
    for p in ICE_WEIGHTS:
        ts = set(series[p].ts.tolist())
        common = ts if common is None else (common & ts)
    if not common:
        raise InsufficientDxyData("no overlapping timestamps across the six pairs")
    ordered = np.array(sorted(common), dtype=np.int64)
    closes = {}
    for p in ICE_WEIGHTS:
        s = series[p]
        idx = np.searchsorted(s.ts, ordered)
        closes[p] = s.close[idx]
    return synthetic_dxy(closes)


@dataclass(frozen=True, slots=True)
class DxyState:
    level: float
    change_1: float
    change_5: float
    change_20: float
    trend: Bias
    percentile: float
    source: str

    @property
    def gold_implication(self) -> Bias:
        """A falling dollar is a bullish gold input, and vice versa."""
        if self.trend is Bias.BULLISH:
            return Bias.BEARISH
        if self.trend is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL


def dxy_state(values: np.ndarray, source: str = "synthetic", trend_bars: int = 20) -> DxyState:
    n = len(values)
    if n < 5:
        raise InsufficientDxyData(f"need at least 5 observations, got {n}")
    level = float(values[-1])

    def pct(k: int) -> float:
        if n <= k or values[-1 - k] == 0:
            return 0.0
        return float((values[-1] / values[-1 - k] - 1.0) * 100.0)

    ch1, ch5, ch20 = pct(1), pct(5), pct(20)
    sl = 0.0
    if n >= trend_bars:
        s = slope(values, trend_bars)
        sl = float(s[-1]) if np.isfinite(s[-1]) else 0.0
    # Normalise the slope by the level so the threshold is scale-free.
    norm = sl / level * 100 * trend_bars if level else 0.0
    if norm > 0.15:
        trend = Bias.BULLISH
    elif norm < -0.15:
        trend = Bias.BEARISH
    else:
        trend = Bias.NEUTRAL

    window = min(100, n)
    recent = values[-window:]
    percentile = float((recent <= level).sum() / window)
    return DxyState(level, ch1, ch5, ch20, trend, percentile, source)


def correlation_with_gold(dxy: np.ndarray, gold: np.ndarray, window: int = 60) -> float:
    """Rolling correlation. Gold and DXY rising TOGETHER is itself a regime signal.

    The textbook inverse relationship breaks down during risk-off episodes and central
    bank buying cycles; treating a breakdown as an error rather than information is a
    common way to be wrong about gold for months at a time.
    """
    n = min(len(dxy), len(gold), window)
    if n < 10:
        return float("nan")
    a, b = np.asarray(dxy[-n:], float), np.asarray(gold[-n:], float)
    da, db = np.diff(a), np.diff(b)
    if da.std() == 0 or db.std() == 0:
        return float("nan")
    return float(np.corrcoef(da, db)[0, 1])
