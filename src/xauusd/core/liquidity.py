"""Liquidity engine: resting pools, sweeps, stop hunts, false breaks.

Core principle enforced throughout: **a liquidity sweep is never, on its own, a trade
signal.** This module answers "was liquidity taken, and how convincingly?" It has no
opinion about whether to trade. That decision needs the sweep plus displacement plus a
market structure shift plus higher-timeframe context plus a valid entry zone plus RR
plus no news conflict — assembled in strategy/setups.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np

from xauusd.config.settings import LiquidityConfig
from xauusd.core.indicators import atr_last
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction, LiquidityKind, Timeframe
from xauusd.domain.types import LiquidityPool, Sweep


def _ts(series: BarSeries, i: int) -> datetime:
    return datetime.fromtimestamp(int(series.ts[i]), UTC)


class LiquidityEngine:
    def __init__(self, config: LiquidityConfig | None = None) -> None:
        self.cfg = config or LiquidityConfig()

    # -- pool construction -------------------------------------------------------------

    def equal_levels(
        self, series: BarSeries, atr_value: float, lookback: int | None = None
    ) -> list[LiquidityPool]:
        """Equal highs / equal lows: clusters of extremes within an ATR-relative band.

        Equal highs are where retail stops sit above and where institutions source
        buy-side liquidity. Tolerance is ATR-relative rather than a fixed number of
        points, so the definition holds across gold's very different volatility eras.
        """
        cfg = self.cfg
        n = len(series)
        if n < 10 or atr_value <= 0:
            return []
        lookback = min(lookback or cfg.sweep_lookback_bars, n)
        start = n - lookback
        tol = cfg.equal_level_tolerance_atr * atr_value
        pools: list[LiquidityPool] = []

        for kind, arr, is_high in (
            (LiquidityKind.EQH, series.high, True),
            (LiquidityKind.EQL, series.low, False),
        ):
            vals = arr[start:]
            order = np.argsort(-vals if is_high else vals)
            used = np.zeros(len(vals), dtype=bool)
            for oi in order:
                if used[oi]:
                    continue
                level = float(vals[oi])
                members = np.flatnonzero((np.abs(vals - level) <= tol) & ~used)
                if len(members) < cfg.min_equal_touches:
                    used[oi] = True
                    continue
                used[members] = True
                extreme = float(np.max(vals[members]) if is_high else np.min(vals[members]))
                first = int(members.min())
                pools.append(
                    LiquidityPool(
                        kind=kind,
                        timeframe=series.timeframe,
                        price=extreme,
                        formed_ts=_ts(series, start + first),
                        price_upper=extreme + tol / 2,
                        price_lower=extreme - tol / 2,
                        touches=len(members),
                        strength=min(1.0, 0.35 + 0.15 * len(members)),
                    )
                )
        return pools

    def swing_pools(self, series: BarSeries, swings: list, atr_value: float) -> list[LiquidityPool]:
        """Untaken structural swing highs/lows are the primary resting liquidity."""
        from xauusd.domain.enums import SwingKind

        pools: list[LiquidityPool] = []
        tol = self.cfg.equal_level_tolerance_atr * atr_value if atr_value > 0 else 0.0
        for s in swings:
            if not getattr(s, "structural", True):
                continue
            is_high = s.kind is SwingKind.HIGH
            pools.append(
                LiquidityPool(
                    kind=LiquidityKind.BSL if is_high else LiquidityKind.SSL,
                    timeframe=series.timeframe,
                    price=s.price,
                    formed_ts=s.ts,
                    price_upper=s.price + tol / 2,
                    price_lower=s.price - tol / 2,
                    touches=1,
                    strength=0.5,
                )
            )
        return pools

    def session_and_period_pools(
        self,
        d1: BarSeries | None,
        w1: BarSeries | None,
        asia_high: float | None = None,
        asia_low: float | None = None,
        asia_ts: datetime | None = None,
    ) -> list[LiquidityPool]:
        """Previous day/week extremes and the Asian range — the reference draws."""
        pools: list[LiquidityPool] = []
        if d1 is not None and len(d1) >= 2:
            pd = d1.bar_at(len(d1) - 1)  # last CLOSED daily bar = previous day
            pools.append(
                LiquidityPool(
                    LiquidityKind.PDH, Timeframe.D1, pd.high, pd.ts, touches=1, strength=0.8
                )
            )
            pools.append(
                LiquidityPool(
                    LiquidityKind.PDL, Timeframe.D1, pd.low, pd.ts, touches=1, strength=0.8
                )
            )
        if w1 is not None and len(w1) >= 2:
            pw = w1.bar_at(len(w1) - 1)
            pools.append(
                LiquidityPool(
                    LiquidityKind.PWH, Timeframe.W1, pw.high, pw.ts, touches=1, strength=0.9
                )
            )
            pools.append(
                LiquidityPool(
                    LiquidityKind.PWL, Timeframe.W1, pw.low, pw.ts, touches=1, strength=0.9
                )
            )
        if asia_high is not None and asia_ts is not None:
            pools.append(
                LiquidityPool(
                    LiquidityKind.SESSION_HIGH,
                    Timeframe.M15,
                    asia_high,
                    asia_ts,
                    touches=1,
                    strength=0.6,
                )
            )
        if asia_low is not None and asia_ts is not None:
            pools.append(
                LiquidityPool(
                    LiquidityKind.SESSION_LOW,
                    Timeframe.M15,
                    asia_low,
                    asia_ts,
                    touches=1,
                    strength=0.6,
                )
            )
        return pools

    # -- sweep detection ---------------------------------------------------------------

    def mark_swept(self, pools: list[LiquidityPool], series: BarSeries) -> list[LiquidityPool]:
        """Flag pools whose level has been traded through. Only resting pools are targets."""
        out: list[LiquidityPool] = []
        for p in pools:
            i0 = series.index_at_or_before(p.formed_ts)
            after = slice(max(i0 + 1, 0), len(series))
            highs, lows = series.high[after], series.low[after]
            if highs.size == 0:
                out.append(p)
                continue
            taken = (
                np.flatnonzero(highs > p.price) if p.is_buyside else np.flatnonzero(lows < p.price)
            )
            if taken.size:
                idx = int(taken[0]) + max(i0 + 1, 0)
                out.append(replace(p, swept_ts=_ts(series, idx)))
            else:
                out.append(p)
        return out

    def detect_sweeps(
        self, series: BarSeries, pools: list[LiquidityPool], atr_value: float
    ) -> list[Sweep]:
        """A sweep: penetration beyond a pool, then rejection back through it.

        Every condition below is necessary; none is sufficient. Quality is scored so
        the strategy layer can require a strong one rather than merely a valid one.
        """
        cfg = self.cfg
        n = len(series)
        if n < 5 or atr_value <= 0:
            return []
        sweeps: list[Sweep] = []
        window_start = max(0, n - cfg.sweep_lookback_bars)

        for pool in pools:
            formed_i = series.index_at_or_before(pool.formed_ts)
            scan_from = max(window_start, formed_i + 1, 1)
            for i in range(scan_from, n):
                hi, lo = float(series.high[i]), float(series.low[i])
                close = float(series.close[i])
                rng = hi - lo
                if rng <= 0:
                    continue

                if pool.is_buyside:
                    if hi <= pool.price:
                        continue
                    penetration = hi - pool.price
                    rejection = (hi - max(close, float(series.open[i]))) / rng
                    closed_inside = close < pool.price
                    direction = Direction.SHORT
                else:
                    if lo >= pool.price:
                        continue
                    penetration = pool.price - lo
                    rejection = (min(close, float(series.open[i])) - lo) / rng
                    closed_inside = close > pool.price
                    direction = Direction.LONG

                pen_atr = penetration / atr_value
                if pen_atr < cfg.sweep_min_penetration_atr:
                    continue
                # A deep move through the level is a genuine break, not a sweep.
                if pen_atr > cfg.sweep_max_penetration_atr:
                    continue

                bars_to_reject = 1
                if not closed_inside:
                    # Allow the rejection to complete over the next few bars.
                    resolved = False
                    for j in range(i + 1, min(i + 1 + cfg.sweep_max_bars_to_reject, n)):
                        cj = float(series.close[j])
                        if (pool.is_buyside and cj < pool.price) or (
                            not pool.is_buyside and cj > pool.price
                        ):
                            bars_to_reject = j - i + 1
                            closed_inside = True
                            resolved = True
                            break
                    if not resolved and cfg.sweep_require_close_back_inside:
                        continue

                if rejection < cfg.sweep_min_rejection_ratio and bars_to_reject == 1:
                    continue

                displacement = self._displacement_after(series, i, direction, atr_value)
                sweeps.append(
                    Sweep(
                        ts=_ts(series, i),
                        timeframe=series.timeframe,
                        pool=pool,
                        direction=direction,
                        penetration=penetration,
                        penetration_atr=pen_atr,
                        rejection_ratio=max(0.0, rejection),
                        closed_back_inside=closed_inside,
                        displacement_after_atr=displacement,
                        bars_to_reject=bars_to_reject,
                    )
                )
                break  # one sweep per pool: the first take is the meaningful one
        sweeps.sort(key=lambda s: s.ts)
        return sweeps

    def _displacement_after(
        self, series: BarSeries, i: int, direction: Direction, atr_value: float, bars: int = 5
    ) -> float:
        """Move away from the sweep in the reversal direction, measured in ATR.

        This is the evidence that the sweep was institutional rather than noise: real
        stop-hunts are followed by a decisive move, not a drift.
        """
        end = min(i + 1 + bars, len(series))
        if end <= i + 1 or atr_value <= 0:
            return 0.0
        ref = float(series.close[i])
        seg = series.low[i + 1 : end] if direction is Direction.SHORT else series.high[i + 1 : end]
        move = (
            (ref - float(np.min(seg)))
            if direction is Direction.SHORT
            else (float(np.max(seg)) - ref)
        )
        return max(0.0, move / atr_value)

    def is_false_breakout(
        self, series: BarSeries, pool: LiquidityPool, atr_value: float, within: int = 5
    ) -> bool:
        """A level broken by a body close but reclaimed shortly afterwards."""
        i0 = series.index_at_or_before(pool.formed_ts)
        for i in range(max(i0 + 1, 0), len(series)):
            c = float(series.close[i])
            broke = c > pool.price if pool.is_buyside else c < pool.price
            if not broke:
                continue
            end = min(i + 1 + within, len(series))
            for j in range(i + 1, end):
                cj = float(series.close[j])
                if (pool.is_buyside and cj < pool.price) or (
                    not pool.is_buyside and cj > pool.price
                ):
                    return True
            return False
        return False

    # -- targets -----------------------------------------------------------------------

    def draw_on_liquidity(
        self, pools: list[LiquidityPool], price: float, direction: Direction
    ) -> list[LiquidityPool]:
        """Resting pools ahead of price in the trade direction — real TP anchors.

        Take-profits are placed at opposing liquidity, not at a round multiple of risk.
        A 1:3 target with nothing behind it is a fantasy; this is what makes the
        difference between choosing 1:2 and 1:3 an evidence-based decision.
        """
        want_buyside = direction is Direction.LONG
        ahead = [
            p
            for p in pools
            if p.is_resting
            and p.is_buyside == want_buyside
            and ((p.price > price) if want_buyside else (p.price < price))
        ]
        ahead.sort(key=lambda p: p.price if want_buyside else -p.price)
        return ahead

    def opposing_liquidity_near(
        self,
        pools: list[LiquidityPool],
        price: float,
        direction: Direction,
        within: float,
    ) -> list[LiquidityPool]:
        """Liquidity sitting AGAINST the trade close to entry — a scoring penalty.

        Buying with sell-side liquidity resting just below means the trade is likely to
        be stopped out on the way to being right.
        """
        want_buyside = direction is Direction.SHORT
        return [
            p
            for p in pools
            if p.is_resting
            and p.is_buyside == want_buyside
            and abs(p.price - price) <= within
            and ((p.price < price) if direction is Direction.LONG else (p.price > price))
        ]

    # -- top level ---------------------------------------------------------------------

    def analyze(
        self,
        series: BarSeries,
        swings: list | None = None,
        d1: BarSeries | None = None,
        w1: BarSeries | None = None,
        asia: tuple[float, float, datetime] | None = None,
    ) -> tuple[list[LiquidityPool], list[Sweep]]:
        a = atr_last(series, 14)
        if not np.isfinite(a) or a <= 0:
            return [], []
        pools = self.equal_levels(series, a)
        if swings:
            pools += self.swing_pools(series, swings, a)
        pools += self.session_and_period_pools(d1, w1, *(asia if asia else (None, None, None)))
        pools = self.mark_swept(pools, series)
        pools = self._dedupe(pools, a)
        sweeps = self.detect_sweeps(series, pools, a)
        return pools, sweeps

    def _dedupe(self, pools: list[LiquidityPool], atr_value: float) -> list[LiquidityPool]:
        """Collapse pools at effectively the same price, keeping the strongest."""
        tol = self.cfg.equal_level_tolerance_atr * atr_value * 0.5
        kept: list[LiquidityPool] = []
        for p in sorted(pools, key=lambda x: (-x.strength, -x.touches)):
            if any(abs(p.price - k.price) <= tol and p.is_buyside == k.is_buyside for k in kept):
                continue
            kept.append(p)
        return sorted(kept, key=lambda p: p.price)
