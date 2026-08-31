"""Walk-forward analysis.

Rolling and anchored windows: optimise (or simply evaluate) on an in-sample window,
then measure on the immediately following out-of-sample window, and step forward.

The headline number is WALK-FORWARD EFFICIENCY — out-of-sample expectancy divided by
in-sample expectancy. Near 1.0 means the in-sample result carried forward; near 0 or
negative means it was curve fitting. Efficiency well ABOVE 1.0 is not good news either;
it usually means the windows differ in regime rather than that the strategy improved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from xauusd.backtesting.metrics import Metrics
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Timeframe


@dataclass(slots=True)
class Window:
    index: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime
    is_metrics: Metrics | None = None
    oos_metrics: Metrics | None = None

    @property
    def efficiency(self) -> float:
        if not self.is_metrics or not self.oos_metrics:
            return 0.0
        if self.is_metrics.expectancy_r == 0:
            return 0.0
        return self.oos_metrics.expectancy_r / self.is_metrics.expectancy_r

    @property
    def oos_profitable(self) -> bool:
        return bool(self.oos_metrics and self.oos_metrics.expectancy_r > 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "is_period": [self.is_start.isoformat(), self.is_end.isoformat()],
            "oos_period": [self.oos_start.isoformat(), self.oos_end.isoformat()],
            "is_trades": self.is_metrics.trades if self.is_metrics else 0,
            "oos_trades": self.oos_metrics.trades if self.oos_metrics else 0,
            "is_expectancy_r": round(self.is_metrics.expectancy_r, 4) if self.is_metrics else 0,
            "oos_expectancy_r": (
                round(self.oos_metrics.expectancy_r, 4) if self.oos_metrics else 0
            ),
            "oos_win_rate": round(self.oos_metrics.win_rate, 4) if self.oos_metrics else 0,
            "efficiency": round(self.efficiency, 4),
            "oos_profitable": self.oos_profitable,
        }


@dataclass(slots=True)
class WalkForwardResult:
    windows: list[Window] = field(default_factory=list)
    anchored: bool = False

    @property
    def efficiency(self) -> float:
        """Aggregate: total OOS expectancy over total IS expectancy, trade-weighted."""
        oos = [w for w in self.windows if w.oos_metrics and w.oos_metrics.trades]
        ins = [w for w in self.windows if w.is_metrics and w.is_metrics.trades]
        if not oos or not ins:
            return 0.0
        oos_total = sum(w.oos_metrics.expectancy_r * w.oos_metrics.trades for w in oos)
        oos_n = sum(w.oos_metrics.trades for w in oos)
        is_total = sum(w.is_metrics.expectancy_r * w.is_metrics.trades for w in ins)
        is_n = sum(w.is_metrics.trades for w in ins)
        if is_n == 0 or oos_n == 0:
            return 0.0
        is_avg = is_total / is_n
        return (oos_total / oos_n) / is_avg if is_avg != 0 else 0.0

    @property
    def profitable_window_fraction(self) -> float:
        scored = [w for w in self.windows if w.oos_metrics and w.oos_metrics.trades > 0]
        if not scored:
            return 0.0
        return sum(1 for w in scored if w.oos_profitable) / len(scored)

    @property
    def total_oos_trades(self) -> int:
        return sum(w.oos_metrics.trades for w in self.windows if w.oos_metrics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchored": self.anchored,
            "windows": [w.as_dict() for w in self.windows],
            "aggregate_efficiency": round(self.efficiency, 4),
            "profitable_window_fraction": round(self.profitable_window_fraction, 4),
            "total_oos_trades": self.total_oos_trades,
        }


def make_windows(
    data: dict[Timeframe, BarSeries],
    base_tf: Timeframe,
    is_bars: int,
    oos_bars: int,
    anchored: bool = False,
    max_windows: int = 20,
) -> list[tuple[dict[Timeframe, BarSeries], dict[Timeframe, BarSeries], Window]]:
    """Build (in-sample data, out-of-sample data, window) triples.

    Slicing is by TIMESTAMP across every timeframe, so an H4 bar can never straddle the
    boundary and leak in-sample information into the out-of-sample window.
    """
    base = data[base_tf]
    n = len(base)
    out = []
    idx = 0
    start = 0
    while True:
        is_lo = 0 if anchored else start
        is_hi = start + is_bars
        oos_hi = is_hi + oos_bars
        if oos_hi > n or idx >= max_windows:
            break
        is_lo_ts, is_hi_ts = int(base.ts[is_lo]), int(base.ts[is_hi])
        oos_hi_ts = int(base.ts[min(oos_hi, n - 1)])

        def cut(lo_ts: int, hi_ts: int) -> dict[Timeframe, BarSeries]:
            sliced = {}
            for tf, s in data.items():
                a = int(np.searchsorted(s.ts, lo_ts, side="left"))
                b = int(np.searchsorted(s.ts, hi_ts, side="right"))
                sliced[tf] = s.slice(a, b)
            return sliced

        window = Window(
            index=idx,
            is_start=base.bar_at(is_lo).ts,
            is_end=base.bar_at(is_hi - 1).ts,
            oos_start=base.bar_at(is_hi).ts,
            oos_end=base.bar_at(min(oos_hi, n - 1) - 1).ts,
        )
        out.append((cut(is_lo_ts, is_hi_ts), cut(is_hi_ts, oos_hi_ts), window))
        idx += 1
        start += oos_bars
    return out


def run(
    data: dict[Timeframe, BarSeries],
    backtest_fn: Callable[[dict[Timeframe, BarSeries]], Metrics],
    base_tf: Timeframe = Timeframe.M5,
    is_bars: int = 20_000,
    oos_bars: int = 6_000,
    anchored: bool = False,
    max_windows: int = 12,
) -> WalkForwardResult:
    result = WalkForwardResult(anchored=anchored)
    for is_data, oos_data, window in make_windows(
        data, base_tf, is_bars, oos_bars, anchored, max_windows
    ):
        try:
            window.is_metrics = backtest_fn(is_data)
            window.oos_metrics = backtest_fn(oos_data)
        except ValueError:
            continue  # window too small after slicing
        result.windows.append(window)
    return result
