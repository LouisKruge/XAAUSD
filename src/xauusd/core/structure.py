"""Market structure engine: swings, BOS, CHOCH, MSS, bias, dealing range.

Implements docs/specs/market_structure.md exactly. The critical property is that a
swing is only usable from the bar at which it became KNOWABLE (`confirmed_index`),
because a fractal is defined by the bars after it. Skipping that is how an SMC
backtest quietly reads the future and produces a 90% win rate that never repeats.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import numpy as np

from xauusd.config.settings import StructureConfig
from xauusd.core.indicators import atr_last
from xauusd.data.series import BarSeries
from xauusd.domain.enums import (
    Bias,
    Direction,
    StructureKind,
    SwingKind,
    SwingStrength,
)
from xauusd.domain.types import DealingRange, StructureEvent, Swing, TimeframeStructure


@dataclass(frozen=True, slots=True)
class RawSwing:
    """Internal swing with its confirmation index — the anti-lookahead field."""

    index: int
    confirmed_index: int
    price: float
    kind: SwingKind
    ts: datetime
    structural: bool = True
    strength: SwingStrength = SwingStrength.UNTESTED


def detect_swings(
    series: BarSeries, lookback: int = 2, min_leg_atr: float = 0.0, atr_value: float = 0.0
) -> list[RawSwing]:
    """Fractal swings with confirmation lag and a significance filter.

    Comparison is strict to the LEFT and non-strict to the RIGHT, which makes the
    definition deterministic on flat tops — exactly where liquidity pools form.
    """
    n = len(series)
    out: list[RawSwing] = []
    if n < 2 * lookback + 1:
        return out

    # Vectorised fractal detection. The naive per-bar loop with np.all was the single
    # largest cost in the decision cycle; sliding windows give identical results.
    win = 2 * lookback + 1
    high_w = np.lib.stride_tricks.sliding_window_view(series.high, win)
    low_w = np.lib.stride_tricks.sliding_window_view(series.low, win)
    centre_h = high_w[:, lookback]
    centre_l = low_w[:, lookback]
    is_high = (centre_h > high_w[:, :lookback].max(axis=1)) & (
        centre_h >= high_w[:, lookback + 1 :].max(axis=1)
    )
    is_low = (centre_l < low_w[:, :lookback].min(axis=1)) & (
        centre_l <= low_w[:, lookback + 1 :].min(axis=1)
    )
    # A bar cannot be both; highs take precedence, matching the original ordering.
    is_low &= ~is_high

    ts = series.ts
    for idx in np.flatnonzero(is_high) + lookback:
        i = int(idx)
        out.append(
            RawSwing(
                i,
                i + lookback,
                float(series.high[i]),
                SwingKind.HIGH,
                datetime.fromtimestamp(int(ts[i]), UTC),
            )
        )
    for idx in np.flatnonzero(is_low) + lookback:
        i = int(idx)
        out.append(
            RawSwing(
                i,
                i + lookback,
                float(series.low[i]),
                SwingKind.LOW,
                datetime.fromtimestamp(int(ts[i]), UTC),
            )
        )

    out.sort(key=lambda s: s.index)
    out = _alternate(out)
    if min_leg_atr > 0 and atr_value > 0:
        threshold = min_leg_atr * atr_value
        out = [replace(s, structural=_leg_size(out, idx) >= threshold) for idx, s in enumerate(out)]
    return out


def _leg_size(swings: list[RawSwing], idx: int) -> float:
    """Distance from the previous opposite swing. First swing has no leg; treat as large."""
    if idx == 0:
        return float("inf")
    return abs(swings[idx].price - swings[idx - 1].price)


def _alternate(swings: list[RawSwing]) -> list[RawSwing]:
    """Enforce high/low alternation, keeping the more extreme of any same-kind run."""
    out: list[RawSwing] = []
    for s in swings:
        if not out or out[-1].kind is not s.kind:
            out.append(s)
            continue
        prev = out[-1]
        better = s.price > prev.price if s.kind is SwingKind.HIGH else s.price < prev.price
        if better:
            out[-1] = s
    return out


def visible_swings(swings: list[RawSwing], at_index: int) -> list[RawSwing]:
    """Only swings CONFIRMED at or before `at_index`. The anti-lookahead filter."""
    return [s for s in swings if s.confirmed_index <= at_index]


class StructureEngine:
    def __init__(self, config: StructureConfig | None = None) -> None:
        self.cfg = config or StructureConfig()

    # -- events ------------------------------------------------------------------------

    def detect_events(
        self, series: BarSeries, swings: list[RawSwing], atr_value: float
    ) -> list[StructureEvent]:
        """Walk forward, emitting BOS / CHOCH / MSS as they become knowable."""
        cfg = self.cfg
        events: list[StructureEvent] = []
        if atr_value <= 0 or not np.isfinite(atr_value):
            return events

        bias = Bias.NEUTRAL

        # Precompute once. Reading series.body_ratio inside the loop recomputed the
        # entire array on every bar and dominated the decision cycle.
        body_ratios = series.body_ratio
        closes = series.close
        highs_arr = series.high
        lows_arr = series.low
        ts_arr = series.ts

        # Swings become usable in confirmation order, so walk a pointer rather than
        # re-filtering the whole swing list on every bar.
        ordered = sorted((s for s in swings if s.structural), key=lambda s: s.confirmed_index)
        ptr = 0
        active_highs: list[RawSwing] = []
        active_lows: list[RawSwing] = []

        bos_disp = cfg.bos_min_displacement_atr * atr_value
        mss_disp = cfg.mss_min_displacement_atr * atr_value

        for k in range(len(series)):
            while ptr < len(ordered) and ordered[ptr].confirmed_index <= k:
                sw = ordered[ptr]
                (active_highs if sw.kind is SwingKind.HIGH else active_lows).append(sw)
                ptr += 1
            if not (active_highs or active_lows):
                continue

            close_k = float(closes[k])
            body_ratio = float(body_ratios[k])
            if body_ratio < cfg.bos_min_body_ratio:
                continue

            # --- bullish break ------------------------------------------------------
            if active_highs:
                ref = active_highs[-1]
                disp = close_k - ref.price
                broke = (
                    close_k > ref.price
                    if cfg.bos_require_body_close
                    else float(highs_arr[k]) > ref.price
                )
                if broke and disp >= bos_disp:
                    # Every swing high at or below the break has been taken, not only
                    # the most recent. Without this, one impulse fires a cascade of BOS
                    # events as the walk falls back onto older, lower highs.
                    while active_highs and active_highs[-1].price <= close_k:
                        active_highs.pop()
                    against = bias is Bias.BEARISH
                    kind = StructureKind.CHOCH if against else StructureKind.BOS
                    if against and disp >= mss_disp:
                        kind = StructureKind.MSS
                    events.append(
                        StructureEvent(
                            ts=datetime.fromtimestamp(int(ts_arr[k]), UTC),
                            timeframe=series.timeframe,
                            kind=kind,
                            direction=Direction.LONG,
                            price=ref.price,
                            break_price=close_k,
                            ref_swing_ts=ref.ts,
                            displacement_atr=disp / atr_value,
                            body_ratio=body_ratio,
                        )
                    )
                    # A CHOCH flips to NEUTRAL, never straight to the opposite bias.
                    bias = Bias.BULLISH if kind is StructureKind.BOS else Bias.NEUTRAL
                    continue

            # --- bearish break ------------------------------------------------------
            if active_lows:
                ref = active_lows[-1]
                disp = ref.price - close_k
                broke = (
                    close_k < ref.price
                    if cfg.bos_require_body_close
                    else float(lows_arr[k]) < ref.price
                )
                if broke and disp >= bos_disp:
                    while active_lows and active_lows[-1].price >= close_k:
                        active_lows.pop()
                    against = bias is Bias.BULLISH
                    kind = StructureKind.CHOCH if against else StructureKind.BOS
                    if against and disp >= mss_disp:
                        kind = StructureKind.MSS
                    events.append(
                        StructureEvent(
                            ts=datetime.fromtimestamp(int(ts_arr[k]), UTC),
                            timeframe=series.timeframe,
                            kind=kind,
                            direction=Direction.SHORT,
                            price=ref.price,
                            break_price=close_k,
                            ref_swing_ts=ref.ts,
                            displacement_atr=disp / atr_value,
                            body_ratio=body_ratio,
                        )
                    )
                    bias = Bias.BEARISH if kind is StructureKind.BOS else Bias.NEUTRAL
        return events

    # -- strength ----------------------------------------------------------------------

    def classify_strength(
        self, series: BarSeries, swings: list[RawSwing], events: list[StructureEvent]
    ) -> list[RawSwing]:
        """A high price failed to take, then broke down from, is STRONG."""
        out: list[RawSwing] = []
        for s in swings:
            after = (
                series.high[s.index + 1 :]
                if s.kind is SwingKind.HIGH
                else series.low[s.index + 1 :]
            )
            if after.size == 0:
                out.append(s)
                continue
            taken = (
                bool(np.any(after > s.price))
                if s.kind is SwingKind.HIGH
                else bool(np.any(after < s.price))
            )
            if taken:
                out.append(replace(s, strength=SwingStrength.WEAK))
                continue
            opposite = Direction.SHORT if s.kind is SwingKind.HIGH else Direction.LONG
            broke_away = any(
                e.ts > s.ts
                and e.direction is opposite
                and e.kind in (StructureKind.BOS, StructureKind.MSS, StructureKind.CHOCH)
                for e in events
            )
            out.append(
                replace(s, strength=SwingStrength.STRONG if broke_away else SwingStrength.UNTESTED)
            )
        return out

    # -- bias & range ------------------------------------------------------------------

    def bias_from(self, swings: list[RawSwing], events: list[StructureEvent]) -> Bias:
        structural = [s for s in swings if s.structural]
        highs = [s for s in structural if s.kind is SwingKind.HIGH]
        lows = [s for s in structural if s.kind is SwingKind.LOW]
        if len(highs) < 2 or len(lows) < 2:
            return Bias.NEUTRAL  # unknown, not "neutral-ish"

        if events:
            last = events[-1]
            if last.kind is StructureKind.BOS:
                return Bias.BULLISH if last.direction is Direction.LONG else Bias.BEARISH
            if last.kind in (StructureKind.CHOCH, StructureKind.MSS):
                return Bias.NEUTRAL

        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
        if hh and hl:
            return Bias.BULLISH
        if lh and ll:
            return Bias.BEARISH
        return Bias.NEUTRAL

    def dealing_range(self, series: BarSeries, swings: list[RawSwing]) -> DealingRange | None:
        """Most recent unbroken structural swing high and low."""
        structural = [s for s in swings if s.structural]
        last_close = float(series.close[-1]) if len(series) else 0.0
        hi = next(
            (s for s in reversed(structural) if s.kind is SwingKind.HIGH and s.price > last_close),
            None,
        )
        lo = next(
            (s for s in reversed(structural) if s.kind is SwingKind.LOW and s.price < last_close),
            None,
        )
        if hi is None:
            hi = next((s for s in reversed(structural) if s.kind is SwingKind.HIGH), None)
        if lo is None:
            lo = next((s for s in reversed(structural) if s.kind is SwingKind.LOW), None)
        if hi is None or lo is None or hi.price <= lo.price:
            return None
        return DealingRange(
            high=hi.price,
            low=lo.price,
            high_ts=hi.ts,
            low_ts=lo.ts,
            timeframe=series.timeframe,
        )

    # -- top level ---------------------------------------------------------------------

    def analyze(self, series: BarSeries, internal: bool = False) -> TimeframeStructure:
        cfg = self.cfg
        tf = series.timeframe
        # Enough history for ATR to be meaningful AND for several swings to have
        # formed. A "bias" derived from 25 bars is noise wearing a label, and it would
        # propagate into the HTF-alignment gate as if it were knowledge.
        min_bars = max(cfg.atr_period * 3, 50)
        if len(series) < min_bars:
            return TimeframeStructure(tf, Bias.NEUTRAL, None, (), None)

        a = atr_last(series, cfg.atr_period)
        if not np.isfinite(a) or a <= 0:
            return TimeframeStructure(tf, Bias.NEUTRAL, None, (), None)

        lookback = cfg.internal_swing_lookback if internal else cfg.swing_lookback
        raw = detect_swings(series, lookback, cfg.swing_min_atr, a)
        raw = raw[-cfg.max_swings_tracked :] if len(raw) > cfg.max_swings_tracked else raw
        events = self.detect_events(series, raw, a)
        raw = self.classify_strength(series, raw, events)

        swings = tuple(
            Swing(
                ts=s.ts,
                index=s.index,
                price=s.price,
                kind=s.kind,
                timeframe=tf,
                strength=s.strength,
                confirmed_ts=datetime.fromtimestamp(
                    int(series.ts[min(s.confirmed_index, len(series) - 1)]), UTC
                ),
            )
            for s in raw
            if s.structural
        )
        last_bos = next((e for e in reversed(events) if e.kind is StructureKind.BOS), None)
        last_choch = next((e for e in reversed(events) if e.kind is StructureKind.CHOCH), None)
        last_mss = next((e for e in reversed(events) if e.kind is StructureKind.MSS), None)

        return TimeframeStructure(
            timeframe=tf,
            bias=self.bias_from(raw, events),
            last_event=events[-1] if events else None,
            swings=swings,
            dealing_range=self.dealing_range(series, raw),
            last_bos=last_bos,
            last_choch=last_choch,
            last_mss=last_mss,
        )

    def recent_mss(self, series: BarSeries, within_bars: int = 20) -> StructureEvent | None:
        """The MSS a setup requires: recent, and in a knowable position."""
        a = atr_last(series, self.cfg.atr_period)
        if not np.isfinite(a) or a <= 0 or len(series) < 20:
            return None
        raw = detect_swings(series, self.cfg.swing_lookback, self.cfg.swing_min_atr, a)
        events = self.detect_events(series, raw, a)
        if not events:
            return None
        cutoff = datetime.fromtimestamp(int(series.ts[max(0, len(series) - within_bars)]), UTC)
        for e in reversed(events):
            if e.kind is StructureKind.MSS and e.ts >= cutoff:
                return e
        return None
