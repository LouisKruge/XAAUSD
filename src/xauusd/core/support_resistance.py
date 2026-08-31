"""Support/resistance, supply/demand, and premium-discount.

Levels are clustered from swing extremes across timeframes and scored on evidence:
how many times price actually reacted, how hard, how recently, and how important the
timeframe is. A level touched once on M15 is not a level; a level defended three times
on D1 is where the trade dies.
"""

from __future__ import annotations

import math

import numpy as np

from xauusd.config.settings import SRConfig
from xauusd.core.indicators import atr_last
from xauusd.core.structure import RawSwing, detect_swings
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction, LevelKind, SwingKind, Timeframe
from xauusd.domain.types import DealingRange, SRLevel

# How much a level on each timeframe matters. Gold respects daily and weekly levels;
# M15 levels are noise by comparison.
TF_WEIGHT: dict[Timeframe, float] = {
    Timeframe.MN1: 1.00,
    Timeframe.W1: 0.90,
    Timeframe.D1: 0.80,
    Timeframe.H4: 0.60,
    Timeframe.H1: 0.40,
    Timeframe.M15: 0.22,
    Timeframe.M5: 0.10,
    Timeframe.M1: 0.05,
}


class SREngine:
    def __init__(self, config: SRConfig | None = None) -> None:
        self.cfg = config or SRConfig()

    def levels_from(
        self, series: BarSeries, swing_lookback: int = 3, atr_value: float | None = None
    ) -> list[SRLevel]:
        """Cluster swing extremes on one timeframe into scored levels."""
        cfg = self.cfg
        a = atr_value if atr_value and atr_value > 0 else atr_last(series, 14)
        n = len(series)
        if n < 20 or not np.isfinite(a) or a <= 0:
            return []

        limit = cfg.lookback_bars.get(str(series.timeframe), 300)
        window = series.tail(min(limit, n))
        swings = detect_swings(window, swing_lookback, 0.0, a)
        if not swings:
            return []

        tol = cfg.cluster_tolerance_atr * a
        last_close = float(window.close[-1])
        clusters: list[list[RawSwing]] = []
        for s in sorted(swings, key=lambda x: x.price):
            if clusters and abs(s.price - clusters[-1][-1].price) <= tol:
                clusters[-1].append(s)
            else:
                clusters.append([s])

        out: list[SRLevel] = []
        for group in clusters:
            if len(group) < cfg.min_touches:
                continue
            prices = [g.price for g in group]
            level = float(np.mean(prices))
            newest = max(group, key=lambda g: g.index)
            highs = sum(1 for g in group if g.kind is SwingKind.HIGH)
            is_resistance = level > last_close
            kind = (
                (LevelKind.RESISTANCE if is_resistance else LevelKind.SUPPORT)
                if highs != len(group) and highs != 0
                else (LevelKind.SUPPLY if highs == len(group) else LevelKind.DEMAND)
            )
            rejection = self._rejection_strength(window, group, a)
            age_bars = len(window) - newest.index
            recency = math.exp(-age_bars / max(cfg.recency_halflife_bars, 1))
            weight = TF_WEIGHT.get(series.timeframe, 0.3)
            importance = round(
                weight * (0.35 * min(len(group) / 4.0, 1.0) + 0.35 * rejection + 0.30 * recency),
                4,
            )
            out.append(
                SRLevel(
                    kind=kind,
                    timeframe=series.timeframe,
                    price=level,
                    band_upper=level + tol / 2,
                    band_lower=level - tol / 2,
                    formed_ts=min(group, key=lambda g: g.index).ts,
                    touches=len(group),
                    last_test_ts=newest.ts,
                    rejection_strength=round(rejection, 4),
                    importance=importance,
                )
            )
        return out

    def _rejection_strength(
        self, series: BarSeries, group: list[RawSwing], atr_value: float
    ) -> float:
        """How hard price actually turned at this level, in ATR, normalised to 0..1."""
        moves: list[float] = []
        for s in group:
            end = min(s.index + 10, len(series))
            if end <= s.index + 1:
                continue
            if s.kind is SwingKind.HIGH:
                moves.append((s.price - float(np.min(series.low[s.index + 1 : end]))) / atr_value)
            else:
                moves.append((float(np.max(series.high[s.index + 1 : end])) - s.price) / atr_value)
        return float(np.clip(np.mean(moves) / 2.0, 0.0, 1.0)) if moves else 0.0

    def build(self, by_tf: dict[Timeframe, BarSeries], max_levels: int = 24) -> list[SRLevel]:
        """Multi-timeframe level map, deduped with the higher timeframe winning."""
        levels: list[SRLevel] = []
        for tf in (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.W1):
            s = by_tf.get(tf)
            if s is not None and len(s) >= 20:
                levels.extend(self.levels_from(s))
        levels.sort(key=lambda x: -x.importance)

        kept: list[SRLevel] = []
        for lv in levels:
            if any(abs(lv.price - k.price) <= (k.band_upper - k.band_lower) for k in kept):
                continue
            kept.append(lv)
            if len(kept) >= max_levels:
                break
        return sorted(kept, key=lambda x: x.price)

    # -- queries used by scoring and target selection -----------------------------------

    def nearest(
        self, levels: list[SRLevel], price: float, above: bool | None = None
    ) -> SRLevel | None:
        cands = [
            lv
            for lv in levels
            if above is None or (lv.price > price if above else lv.price < price)
        ]
        return min(cands, key=lambda lv: abs(lv.price - price)) if cands else None

    def confluence_at(self, levels: list[SRLevel], price: float, tolerance: float) -> list[SRLevel]:
        return [lv for lv in levels if abs(lv.price - price) <= tolerance]

    def blocking_level(
        self,
        levels: list[SRLevel],
        entry: float,
        target: float,
        direction: Direction,
        min_importance: float = 0.35,
    ) -> SRLevel | None:
        """A significant level sitting between entry and target.

        This is what stops the system placing a 1:3 target on the far side of a daily
        level that has held four times. If one is found, the target is moved in front
        of it or the trade is rejected for RR.
        """
        lo, hi = (entry, target) if direction is Direction.LONG else (target, entry)
        blocking = [lv for lv in levels if lo < lv.price < hi and lv.importance >= min_importance]
        if not blocking:
            return None
        return min(blocking, key=lambda lv: abs(lv.price - entry))


def premium_discount(dealing_range: DealingRange | None, price: float) -> tuple[float, str]:
    """(position 0..1, label). 0 = range low, 1 = range high."""
    if dealing_range is None or dealing_range.size <= 0:
        return 0.5, "UNKNOWN"
    return dealing_range.position_of(price), dealing_range.zone_label(price)


def is_correct_side(
    dealing_range: DealingRange | None,
    price: float,
    direction: Direction,
    tolerance: float = 0.0,
) -> bool:
    """Longs belong in discount, shorts in premium.

    `tolerance` permits trading slightly through equilibrium (e.g. 0.05 allows a long
    up to position 0.55) because a hard boundary at exactly 0.5 rejects good setups on
    a rounding error.
    """
    if dealing_range is None or dealing_range.size <= 0:
        return True  # unknown range does not veto; scoring handles the uncertainty
    pos = dealing_range.position_of(price)
    return pos <= 0.5 + tolerance if direction is Direction.LONG else pos >= 0.5 - tolerance
