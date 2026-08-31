"""Fair Value Gap engine.

Definition (3-bar, strict):
    bullish FVG at bar i:   low[i+1]  > high[i-1]     gap = (high[i-1], low[i+1])
    bearish FVG at bar i:   high[i+1] < low[i-1]      gap = (high[i+1], low[i-1])

The gap is the imbalance left by bar i's displacement. It is only interesting when bar
i actually displaced — a gap left by three small bars drifting is not an institutional
footprint, so `min_displacement_atr` is a required condition, not a scoring nicety.

Lifecycle is tracked because an FVG's value decays as it fills:
    UNMITIGATED -> PARTIAL -> MITIGATED -> INVALIDATED (fully traded through)
                          \-> INVERTED (traded through and now acting as the opposite)

**Never trade an FVG simply because one exists.** This module scores quality; the
strategy layer decides.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from xauusd.config.settings import FVGConfig
from xauusd.core.indicators import atr_last
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction, FVGState
from xauusd.domain.types import FVG, DealingRange


class FVGEngine:
    def __init__(self, config: FVGConfig | None = None) -> None:
        self.cfg = config or FVGConfig()

    def detect(self, series: BarSeries, atr_value: float | None = None) -> list[FVG]:
        cfg = self.cfg
        n = len(series)
        if n < 4:
            return []
        a = atr_value if atr_value and atr_value > 0 else atr_last(series, 14)
        if not np.isfinite(a) or a <= 0:
            return []

        out: list[FVG] = []
        oldest = max(1, n - cfg.max_age_bars)
        for i in range(oldest, n - 1):
            body = abs(float(series.close[i]) - float(series.open[i]))
            disp_atr = body / a

            # bullish: the low two bars later is above the high two bars earlier
            gap_lo, gap_hi = float(series.high[i - 1]), float(series.low[i + 1])
            if gap_hi > gap_lo:
                size = gap_hi - gap_lo
                if size / a >= cfg.min_size_atr and disp_atr >= cfg.min_displacement_atr:
                    out.append(
                        self._build(series, i, Direction.LONG, gap_hi, gap_lo, size, a, disp_atr)
                    )
                    continue

            # bearish: the high two bars later is below the low two bars earlier
            gap_hi_b, gap_lo_b = float(series.low[i - 1]), float(series.high[i + 1])
            if gap_lo_b < gap_hi_b:
                size = gap_hi_b - gap_lo_b
                if size / a >= cfg.min_size_atr and disp_atr >= cfg.min_displacement_atr:
                    out.append(
                        self._build(
                            series, i, Direction.SHORT, gap_hi_b, gap_lo_b, size, a, disp_atr
                        )
                    )
        return out

    def _build(
        self,
        series: BarSeries,
        i: int,
        direction: Direction,
        top: float,
        bottom: float,
        size: float,
        atr_value: float,
        disp_atr: float,
    ) -> FVG:
        state, filled, first_touch = self._lifecycle(series, i + 2, direction, top, bottom)
        return FVG(
            timeframe=series.timeframe,
            direction=direction,
            formed_ts=datetime.fromtimestamp(int(series.ts[i]), UTC),
            top=top,
            bottom=bottom,
            size=size,
            size_atr=size / atr_value,
            displacement_atr=disp_atr,
            state=state,
            mitigated_pct=filled,
            first_touch_ts=first_touch,
        )

    def _lifecycle(
        self, series: BarSeries, from_index: int, direction: Direction, top: float, bottom: float
    ) -> tuple[FVGState, float, datetime | None]:
        """Walk forward from formation to determine how much of the gap has been filled."""
        n = len(series)
        if from_index >= n:
            return FVGState.UNMITIGATED, 0.0, None
        size = top - bottom
        if size <= 0:
            return FVGState.INVALIDATED, 1.0, None

        first_touch: datetime | None = None
        max_fill = 0.0
        for j in range(from_index, n):
            hi, lo, close = float(series.high[j]), float(series.low[j]), float(series.close[j])
            if hi < bottom or lo > top:
                continue
            if first_touch is None:
                first_touch = datetime.fromtimestamp(int(series.ts[j]), UTC)
            if direction is Direction.LONG:
                # A bullish gap fills downward from its top.
                penetration = top - max(lo, bottom)
                if close < bottom:
                    return FVGState.INVERTED, 1.0, first_touch
            else:
                penetration = min(hi, top) - bottom
                if close > top:
                    return FVGState.INVERTED, 1.0, first_touch
            max_fill = max(max_fill, penetration / size)

        if max_fill >= 0.999 and self.cfg.invalidate_on_full_fill:
            return FVGState.INVALIDATED, 1.0, first_touch
        if max_fill >= self.cfg.mitigation_threshold:
            return FVGState.MITIGATED, max_fill, first_touch
        if max_fill > 0:
            return FVGState.PARTIAL, max_fill, first_touch
        return FVGState.UNMITIGATED, 0.0, first_touch

    # -- scoring -----------------------------------------------------------------------

    def score(
        self,
        fvg: FVG,
        dealing_range: DealingRange | None = None,
        has_ob_confluence: bool = False,
        swept_liquidity: bool = False,
        htf_aligned: bool = False,
    ) -> float:
        """0..1 quality. Displacement dominates: it is the actual institutional evidence."""
        s = 0.0
        s += 0.30 * min(fvg.displacement_atr / 1.5, 1.0)
        # Size helps up to a point; an enormous gap means a bad entry and a wide stop.
        s += 0.15 * min(fvg.size_atr / 0.75, 1.0)
        s += {
            FVGState.UNMITIGATED: 0.20,
            FVGState.PARTIAL: 0.14,
            FVGState.MITIGATED: 0.04,
            FVGState.INVERTED: 0.06,
            FVGState.INVALIDATED: 0.0,
        }[fvg.state]
        if dealing_range is not None:
            pos = dealing_range.position_of(fvg.midpoint)
            correct = pos < 0.5 if fvg.direction is Direction.LONG else pos > 0.5
            depth = abs(pos - 0.5) * 2
            s += 0.15 * (depth if correct else 0.0)
        if has_ob_confluence:
            s += 0.10
        if swept_liquidity:
            s += 0.05
        if htf_aligned:
            s += 0.05
        return round(min(s, 1.0), 4)

    def tradable(
        self, fvgs: list[FVG], direction: Direction, price: float, max_distance: float
    ) -> list[FVG]:
        """Unfilled gaps in the trade direction that price could realistically reach."""
        out = [
            f
            for f in fvgs
            if f.direction is direction
            and f.is_tradable
            and abs(f.midpoint - price) <= max_distance
        ]
        out.sort(key=lambda f: abs(f.midpoint - price))
        return out

    def entry_price(self, fvg: FVG) -> float:
        """Consequent encroachment (the 50%) by default, else the far edge.

        The midpoint is a materially better fill than the near edge and materially more
        likely to be reached than the far edge.
        """
        if self.cfg.prefer_consequent_encroachment:
            return fvg.midpoint
        return fvg.bottom if fvg.direction is Direction.LONG else fvg.top
