"""Triple-barrier labelling.

For each candidate, walk FORWARD from the decision bar until one of three barriers is
touched:

    upper barrier   entry +2R   -> label 1 (the setup worked)
    lower barrier   entry -1R   -> label 0 (the setup failed)
    vertical        N bars      -> label by the sign of the outcome at expiry

This is the only place in the system that looks forward, and it is deliberately
quarantined outside the decision path: labels are computed AFTER the fact for training,
and no label or anything derived from one is ever a feature. `MarketView` cannot reach
forward at all, which is what keeps that separation structural rather than a convention.

The conservative intrabar rule from the backtester applies here too: when both barriers
fall inside one bar, the LOSS is taken unless M1 data proves otherwise. Labelling the
favourable side would manufacture an edge the model would then learn to expect.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction


@dataclass(frozen=True, slots=True)
class Label:
    outcome: int  # 1 = target first, 0 = stop first
    r_multiple: float
    bars_to_resolution: int
    resolved_at: datetime | None
    hit: str  # TARGET | STOP | TIMEOUT
    mae_r: float = 0.0
    mfe_r: float = 0.0

    @property
    def is_win(self) -> bool:
        return self.outcome == 1


def triple_barrier(
    series: BarSeries,
    entry_index: int,
    entry: float,
    stop: float,
    direction: Direction,
    target_r: float = 2.0,
    max_bars: int = 96,
    m1: BarSeries | None = None,
) -> Label | None:
    """Label one candidate. Returns None when the horizon runs past the data."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    end = min(entry_index + 1 + max_bars, len(series))
    if entry_index + 1 >= len(series):
        return None

    target = entry + direction.sign * target_r * risk
    mae = mfe = 0.0

    for i in range(entry_index + 1, end):
        hi, lo = float(series.high[i]), float(series.low[i])
        adverse = lo if direction is Direction.LONG else hi
        favour = hi if direction is Direction.LONG else lo
        mae = max(mae, (entry - adverse) * direction.sign / risk)
        mfe = max(mfe, (favour - entry) * direction.sign / risk)

        hit_stop = lo <= stop <= hi
        hit_target = lo <= target <= hi

        if hit_stop and hit_target:
            # Ambiguous bar: resolve with M1 if available, otherwise take the LOSS.
            resolved = _resolve_with_m1(m1, series, i, stop, target, direction)
            if resolved == "TARGET":
                return Label(1, target_r, i - entry_index, series.bar_at(i).ts, "TARGET", mae, mfe)
            return Label(0, -1.0, i - entry_index, series.bar_at(i).ts, "STOP", mae, mfe)
        if hit_stop:
            return Label(0, -1.0, i - entry_index, series.bar_at(i).ts, "STOP", mae, mfe)
        if hit_target:
            return Label(1, target_r, i - entry_index, series.bar_at(i).ts, "TARGET", mae, mfe)

    if end <= entry_index + 1:
        return None
    if end < entry_index + 1 + max_bars:
        return None  # ran out of data: an unresolved label is not a zero label

    final = float(series.close[end - 1])
    r = (final - entry) * direction.sign / risk
    return Label(
        1 if r > 0 else 0, r, end - 1 - entry_index, series.bar_at(end - 1).ts, "TIMEOUT", mae, mfe
    )


def _resolve_with_m1(
    m1: BarSeries | None,
    series: BarSeries,
    i: int,
    stop: float,
    target: float,
    direction: Direction,
) -> str:
    if m1 is None or not len(m1):
        return "STOP"
    t0 = int(series.ts[i])
    t1 = t0 + series.timeframe.seconds
    mask = (m1.ts >= t0) & (m1.ts < t1)
    idx = np.flatnonzero(mask)
    for j in idx:
        j = int(j)
        hi, lo = float(m1.high[j]), float(m1.low[j])
        s_hit = lo <= stop <= hi
        t_hit = lo <= target <= hi
        if s_hit and t_hit:
            return "STOP"  # still ambiguous at M1: stay conservative
        if s_hit:
            return "STOP"
        if t_hit:
            return "TARGET"
    return "STOP"


@dataclass(slots=True)
class LabelledSample:
    ts: datetime
    features: dict[str, float]
    label: int
    r_multiple: float
    resolved_at: datetime | None
    strategy: str
    score: float


def build_dataset(
    samples: Sequence[LabelledSample], feature_names: list[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(X, y, event_times, resolution_times) — the last two drive purged CV."""
    X = np.array(
        [[float(s.features.get(n, 0.0)) for n in feature_names] for s in samples],
        dtype=np.float64,
    )
    y = np.array([s.label for s in samples], dtype=np.int64)
    t0 = np.array([s.ts.timestamp() for s in samples], dtype=np.float64)
    t1 = np.array([(s.resolved_at or s.ts).timestamp() for s in samples], dtype=np.float64)
    return X, y, t0, t1
