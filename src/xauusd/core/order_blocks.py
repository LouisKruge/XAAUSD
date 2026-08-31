"""Order block and breaker engine.

An order block is the last opposing candle before a displacement that BROKE STRUCTURE.
That last clause is the whole point: without a resulting BOS/MSS, "the last down candle
before an up move" is just a candle. `require_bos` defaults to true and should stay
true — dropping it roughly triples the number of zones and destroys their meaning.

  BULL_OB      last DOWN candle before bullish displacement that broke structure
  BEAR_OB      last UP candle before bearish displacement that broke structure
  BULL_BREAKER a failed bearish OB, now flipped to support
  BEAR_BREAKER a failed bullish OB, now flipped to resistance
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from xauusd.config.settings import OrderBlockConfig
from xauusd.core.indicators import atr_last
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction, OrderBlockKind, StructureKind, ZoneState
from xauusd.domain.types import FVG, DealingRange, OrderBlock, StructureEvent


class OrderBlockEngine:
    def __init__(self, config: OrderBlockConfig | None = None) -> None:
        self.cfg = config or OrderBlockConfig()

    def detect(
        self,
        series: BarSeries,
        events: list[StructureEvent],
        atr_value: float | None = None,
        fvgs: list[FVG] | None = None,
    ) -> list[OrderBlock]:
        cfg = self.cfg
        n = len(series)
        if n < 5:
            return []
        a = atr_value if atr_value and atr_value > 0 else atr_last(series, 14)
        if not np.isfinite(a) or a <= 0:
            return []

        out: list[OrderBlock] = []
        structural = [
            e
            for e in events
            if e.kind in (StructureKind.BOS, StructureKind.MSS, StructureKind.CHOCH)
        ]
        if cfg.require_bos and not structural:
            return []

        for event in structural:
            k = series.index_at_or_before(event.ts)
            if k <= 0:
                continue
            ob_index = self._last_opposing_candle(series, k, event.direction)
            if ob_index is None:
                continue
            if event.displacement_atr < cfg.min_displacement_atr:
                continue

            bull = event.direction is Direction.LONG
            top = (
                float(series.high[ob_index])
                if cfg.use_wick_extremes
                else float(max(series.open[ob_index], series.close[ob_index]))
            )
            bottom = (
                float(series.low[ob_index])
                if cfg.use_wick_extremes
                else float(min(series.open[ob_index], series.close[ob_index]))
            )
            state, tests = self._lifecycle(series, ob_index + 1, bull, top, bottom)
            has_fvg = self._has_fvg_overlap(fvgs or [], top, bottom, event.direction)

            out.append(
                OrderBlock(
                    kind=OrderBlockKind.BULL_OB if bull else OrderBlockKind.BEAR_OB,
                    timeframe=series.timeframe,
                    direction=event.direction,
                    formed_ts=datetime.fromtimestamp(int(series.ts[ob_index]), UTC),
                    top=top,
                    bottom=bottom,
                    open_price=float(series.open[ob_index]),
                    close_price=float(series.close[ob_index]),
                    displacement_atr=event.displacement_atr,
                    caused_bos=event.kind in (StructureKind.BOS, StructureKind.MSS),
                    has_fvg=has_fvg,
                    state=state,
                    test_count=tests,
                )
            )
        return self._dedupe(out)

    def _last_opposing_candle(
        self, series: BarSeries, break_index: int, direction: Direction
    ) -> int | None:
        """Walk back from the break to the last candle against the displacement."""
        lo = max(0, break_index - self.cfg.max_lookback_bars)
        want_bear = direction is Direction.LONG  # a bull move originates from a down candle
        for i in range(break_index, lo - 1, -1):
            is_bear = float(series.close[i]) < float(series.open[i])
            if is_bear == want_bear:
                return i
        return None

    def _lifecycle(
        self, series: BarSeries, from_index: int, bull: bool, top: float, bottom: float
    ) -> tuple[ZoneState, int]:
        n = len(series)
        tests = 0
        for j in range(from_index, n):
            hi, lo, close = float(series.high[j]), float(series.low[j]), float(series.close[j])
            if self.cfg.invalidate_on_body_close_through:
                if bull and close < bottom:
                    return ZoneState.INVALIDATED, tests
                if not bull and close > top:
                    return ZoneState.INVALIDATED, tests
            if hi >= bottom and lo <= top:
                tests += 1
        if tests == 0:
            return ZoneState.FRESH, 0
        if tests > self.cfg.max_tests_before_stale:
            return ZoneState.MITIGATED, tests
        return ZoneState.TESTED, tests

    @staticmethod
    def _has_fvg_overlap(fvgs: list[FVG], top: float, bottom: float, direction: Direction) -> bool:
        return any(f.direction is direction and f.bottom <= top and f.top >= bottom for f in fvgs)

    def _dedupe(self, obs: list[OrderBlock]) -> list[OrderBlock]:
        kept: list[OrderBlock] = []
        for ob in sorted(obs, key=lambda o: o.formed_ts):
            if any(
                o.direction is ob.direction and abs(o.midpoint - ob.midpoint) < 1e-9 for o in kept
            ):
                continue
            kept.append(ob)
        return kept

    # -- breakers ----------------------------------------------------------------------

    def detect_breakers(self, series: BarSeries, obs: list[OrderBlock]) -> list[OrderBlock]:
        """An invalidated OB whose level price has since respected is a breaker.

        A bullish OB that failed becomes resistance; a bearish OB that failed becomes
        support. These are often better zones than fresh OBs because the failure itself
        trapped participants.
        """
        out: list[OrderBlock] = []
        for ob in obs:
            if ob.state is not ZoneState.INVALIDATED:
                continue
            i0 = series.index_at_or_before(ob.formed_ts)
            after = slice(max(i0 + 1, 0), len(series))
            highs, lows, closes = series.high[after], series.low[after], series.close[after]
            if closes.size < 3:
                continue
            flipped = (
                OrderBlockKind.BEAR_BREAKER
                if ob.direction is Direction.LONG
                else OrderBlockKind.BULL_BREAKER
            )
            new_dir = ob.direction.opposite
            retested = bool(np.any((highs >= ob.bottom) & (lows <= ob.top)))
            if not retested:
                continue
            out.append(
                OrderBlock(
                    kind=flipped,
                    timeframe=ob.timeframe,
                    direction=new_dir,
                    formed_ts=ob.formed_ts,
                    top=ob.top,
                    bottom=ob.bottom,
                    open_price=ob.open_price,
                    close_price=ob.close_price,
                    displacement_atr=ob.displacement_atr,
                    caused_bos=ob.caused_bos,
                    has_fvg=ob.has_fvg,
                    state=ZoneState.TESTED,
                    test_count=ob.test_count,
                )
            )
        return out

    # -- scoring -----------------------------------------------------------------------

    def score(
        self,
        ob: OrderBlock,
        dealing_range: DealingRange | None = None,
        swept_liquidity: bool = False,
        htf_aligned: bool = False,
        atr_value: float = 0.0,
    ) -> float:
        s = 0.0
        s += 0.28 * min(ob.displacement_atr / 1.5, 1.0)
        s += 0.18 if ob.caused_bos else 0.0
        s += {
            ZoneState.FRESH: 0.18,
            ZoneState.TESTED: 0.10,
            ZoneState.MITIGATED: 0.02,
            ZoneState.INVALIDATED: 0.0,
        }[ob.state]
        s += 0.10 if ob.has_fvg else 0.0
        s += 0.09 if swept_liquidity else 0.0
        if dealing_range is not None:
            pos = dealing_range.position_of(ob.midpoint)
            correct = pos < 0.5 if ob.direction is Direction.LONG else pos > 0.5
            s += 0.12 * (abs(pos - 0.5) * 2 if correct else 0.0)
        s += 0.05 if htf_aligned else 0.0
        # A zone wider than ~1.5 ATR forces an unreasonably wide stop.
        if atr_value > 0 and ob.height > 1.5 * atr_value:
            s *= 0.75
        return round(min(s, 1.0), 4)

    def tradable(
        self, obs: list[OrderBlock], direction: Direction, price: float, max_distance: float
    ) -> list[OrderBlock]:
        out = [
            o
            for o in obs
            if o.direction is direction
            and o.is_tradable
            and abs(o.midpoint - price) <= max_distance
        ]
        out.sort(key=lambda o: abs(o.midpoint - price))
        return out
