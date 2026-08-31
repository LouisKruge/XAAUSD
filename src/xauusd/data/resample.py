"""Timeframe aggregation.

Higher timeframes are DERIVED from a base series rather than fetched independently, so
every timeframe is guaranteed consistent with every other. Two places this matters:

  * a backtest that reads an H4 bias inconsistent with the M5 bars it trades is
    measuring nothing;
  * bucket boundaries must be anchored to real session/calendar boundaries, not to the
    first bar in the file, or the D1 bar the engine sees will not match the broker's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from xauusd.data.series import BarSeries
from xauusd.domain.enums import Timeframe

# Gold's daily bar convention: brokers roll at 22:00 UTC (17:00 New York). Anchoring to
# UTC midnight instead would produce daily highs and lows that do not match the broker's.
DAILY_ROLL_HOUR_UTC = 22


def bucket_start(ts: int, tf: Timeframe, daily_roll_hour: int = DAILY_ROLL_HOUR_UTC) -> int:
    """Epoch seconds of the bucket containing `ts`, anchored correctly per timeframe."""
    if tf in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4):
        return (ts // tf.seconds) * tf.seconds
    dt = datetime.fromtimestamp(ts, UTC)
    if tf is Timeframe.D1:
        anchor = dt.replace(hour=daily_roll_hour, minute=0, second=0, microsecond=0)
        if dt < anchor:
            anchor -= timedelta(days=1)
        return int(anchor.timestamp())
    if tf is Timeframe.W1:
        # Trading week opens Sunday 22:00 UTC.
        days_back = (dt.weekday() - 6) % 7
        anchor = (dt - timedelta(days=days_back)).replace(
            hour=daily_roll_hour, minute=0, second=0, microsecond=0
        )
        if anchor > dt:
            anchor -= timedelta(days=7)
        return int(anchor.timestamp())
    if tf is Timeframe.MN1:
        anchor = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(anchor.timestamp())
    raise ValueError(f"unsupported timeframe {tf}")


def resample(series: BarSeries, target: Timeframe, drop_partial: bool = True) -> BarSeries:
    """Aggregate to a higher timeframe.

    `drop_partial` removes the final bucket unless it is provably complete. Keeping a
    partial higher-timeframe bar is a look-ahead bug in disguise: an H4 bar built from
    one hour of data is not the H4 bar the market will print.
    """
    if target.rank <= series.timeframe.rank:
        raise ValueError(f"cannot resample {series.timeframe} up to {target}")
    n = len(series)
    if n == 0:
        return BarSeries.empty(target)

    keys = np.fromiter((bucket_start(int(t), target) for t in series.ts), dtype=np.int64, count=n)
    boundaries = np.flatnonzero(np.diff(keys)) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [n]))

    ts = keys[starts]
    open_ = series.open[starts]
    close = series.close[ends - 1]
    high = np.maximum.reduceat(series.high, starts)
    low = np.minimum.reduceat(series.low, starts)
    vol = np.add.reduceat(series.tick_volume, starts)
    spread = np.maximum.reduceat(series.spread, starts)

    if drop_partial and len(ts):
        last_bar_close = int(series.ts[-1]) + series.timeframe.seconds
        if last_bar_close < int(ts[-1]) + target.seconds:
            ts, open_, high, low, close, vol, spread = (
                a[:-1] for a in (ts, open_, high, low, close, vol, spread)
            )

    out = BarSeries(
        target,
        ts.astype(np.int64),
        open_.astype(np.float64),
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        vol.astype(np.int64),
        spread.astype(np.float64),
    )
    return out


def build_timeframes(base: BarSeries, targets: list[Timeframe]) -> dict[Timeframe, BarSeries]:
    """Derive a full timeframe set from one base series, all mutually consistent."""
    out: dict[Timeframe, BarSeries] = {base.timeframe: base}
    for tf in sorted(targets, key=lambda t: t.rank):
        if tf.rank <= base.timeframe.rank:
            continue
        out[tf] = resample(base, tf)
    return out
