"""Immutable, numpy-backed bar series.

Analysis runs on typed arrays rather than DataFrames in the hot path: a decision cycle
touches seven timeframes and must finish inside two seconds.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar


@dataclass(frozen=True, slots=True)
class BarSeries:
    """A contiguous run of bars on one timeframe.

    Index 0 is the OLDEST bar; index -1 is the most recent. All arrays share a length.
    """

    timeframe: Timeframe
    ts: np.ndarray  # int64 epoch seconds, ascending, strictly increasing
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    tick_volume: np.ndarray
    spread: np.ndarray

    def __len__(self) -> int:
        return int(self.ts.shape[0])

    def __bool__(self) -> bool:
        return len(self) > 0

    @classmethod
    def from_bars(cls, tf: Timeframe, bars: Sequence[Bar]) -> BarSeries:
        n = len(bars)
        out = cls(
            timeframe=tf,
            ts=np.fromiter((int(b.ts.timestamp()) for b in bars), dtype=np.int64, count=n),
            open=np.fromiter((b.open for b in bars), dtype=np.float64, count=n),
            high=np.fromiter((b.high for b in bars), dtype=np.float64, count=n),
            low=np.fromiter((b.low for b in bars), dtype=np.float64, count=n),
            close=np.fromiter((b.close for b in bars), dtype=np.float64, count=n),
            tick_volume=np.fromiter((b.tick_volume for b in bars), dtype=np.int64, count=n),
            spread=np.fromiter((b.spread_points for b in bars), dtype=np.float64, count=n),
        )
        if n > 1 and not np.all(np.diff(out.ts) > 0):
            raise ValueError(f"{tf}: bar timestamps must be strictly increasing")
        for arr in (out.open, out.high, out.low, out.close):
            arr.setflags(write=False)
        out.ts.setflags(write=False)
        return out

    @classmethod
    def empty(cls, tf: Timeframe) -> BarSeries:
        z64 = np.zeros(0, dtype=np.int64)
        z = np.zeros(0, dtype=np.float64)
        return cls(tf, z64, z, z, z, z, z64, z)

    def slice(self, start: int | None = None, stop: int | None = None) -> BarSeries:
        s = slice(start, stop)
        return BarSeries(
            self.timeframe,
            self.ts[s],
            self.open[s],
            self.high[s],
            self.low[s],
            self.close[s],
            self.tick_volume[s],
            self.spread[s],
        )

    def tail(self, n: int) -> BarSeries:
        return self.slice(max(0, len(self) - n), None) if n > 0 else self

    def bar_at(self, i: int) -> Bar:
        return Bar(
            ts=datetime.fromtimestamp(int(self.ts[i]), UTC),
            open=float(self.open[i]),
            high=float(self.high[i]),
            low=float(self.low[i]),
            close=float(self.close[i]),
            tick_volume=int(self.tick_volume[i]),
            spread_points=int(self.spread[i]),
        )

    def __iter__(self) -> Iterator[Bar]:
        for i in range(len(self)):
            yield self.bar_at(i)

    def to_bars(self) -> list[Bar]:
        return list(self)

    @property
    def last(self) -> Bar:
        if not len(self):
            raise IndexError("empty series")
        return self.bar_at(len(self) - 1)

    @property
    def last_ts(self) -> datetime:
        return datetime.fromtimestamp(int(self.ts[-1]), UTC)

    @property
    def last_close(self) -> float:
        return float(self.close[-1])

    def close_time(self, i: int) -> datetime:
        """When the bar at i finished. This is what 'closed' means everywhere."""
        return datetime.fromtimestamp(int(self.ts[i]) + self.timeframe.seconds, UTC)

    def index_at_or_before(self, when: datetime) -> int:
        """Index of the last bar whose OPEN is at or before `when`; -1 if none."""
        target = int(when.timestamp())
        return int(np.searchsorted(self.ts, target, side="right")) - 1

    @property
    def body(self) -> np.ndarray:
        return np.abs(self.close - self.open)

    @property
    def range(self) -> np.ndarray:
        return self.high - self.low

    @property
    def body_ratio(self) -> np.ndarray:
        r = self.range
        return np.divide(self.body, r, out=np.zeros_like(r), where=r > 0)

    @property
    def is_bull(self) -> np.ndarray:
        return self.close > self.open

    @property
    def hl2(self) -> np.ndarray:
        return (self.high + self.low) / 2.0
