"""Market regime and volatility classification.

Purpose: a strategy may only operate in regimes where it has historically been
profitable, and ABNORMAL is never tradable by anything. Classification is deliberately
coarse — five trend states plus two special ones — because finer distinctions do not
survive out-of-sample.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from xauusd.config.settings import RegimeConfig
from xauusd.core.indicators import (
    adx,
    atr,
    atr_last,
    ema,
    last_valid,
    percentile_rank,
    realized_volatility,
    slope,
)
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Regime, VolRegime
from xauusd.domain.types import VolatilityState


@dataclass(frozen=True, slots=True)
class RegimeResult:
    regime: Regime
    vol_regime: VolRegime
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    atr_percentile: float
    atr_median_ratio: float
    ema_slope: float
    reasons: tuple[str, ...]

    @property
    def is_tradable(self) -> bool:
        return self.regime.is_tradable


class RegimeEngine:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.cfg = config or RegimeConfig()

    def volatility_regime(self, percentile: float) -> VolRegime:
        c = self.cfg
        if np.isnan(percentile):
            return VolRegime.NORMAL
        if percentile >= c.vol_percentile_extreme:
            return VolRegime.EXTREME
        if percentile >= c.vol_percentile_high:
            return VolRegime.HIGH
        if percentile <= c.vol_percentile_low:
            return VolRegime.LOW
        return VolRegime.NORMAL

    def classify(
        self, series: BarSeries, spread_points: float = 0.0, spread_median: float = 0.0
    ) -> RegimeResult:
        """Classify from H1 (or H4) structure. Insufficient history -> ABNORMAL, not a guess."""
        c = self.cfg
        reasons: list[str] = []
        n = len(series)
        min_bars = max(c.vol_window_bars // 4, c.adx_period * 3, 60)
        if n < min_bars:
            return RegimeResult(
                Regime.ABNORMAL,
                VolRegime.NORMAL,
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
                float("nan"),
                (f"insufficient history: {n} bars < {min_bars} required",),
            )

        adx_v, plus_di, minus_di = adx(series, c.adx_period)
        adx_now = last_valid(adx_v, 0.0)
        p_di = last_valid(plus_di, 0.0)
        m_di = last_valid(minus_di, 0.0)

        atr_arr = atr(series, 14)
        atr_now = last_valid(atr_arr)
        window = min(c.vol_window_bars, n - 1)
        atr_pct = last_valid(
            percentile_rank(atr_arr[~np.isnan(atr_arr)], min(window, 100)), float("nan")
        )
        finite_atr = atr_arr[np.isfinite(atr_arr)]
        atr_median = float(np.median(finite_atr[-window:])) if finite_atr.size else float("nan")
        atr_ratio = atr_now / atr_median if atr_median and atr_median > 0 else float("nan")

        ema50 = ema(series.close, 50)
        slope_v = last_valid(slope(ema50[np.isfinite(ema50)], 20), 0.0)
        norm_slope = slope_v / atr_now if atr_now and atr_now > 0 else 0.0

        vol_regime = self.volatility_regime(atr_pct)

        # --- ABNORMAL takes precedence over everything -------------------------------
        # A market this far outside its own recent behaviour is not one whose structure
        # we can claim to read, regardless of what ADX says.
        if np.isfinite(atr_ratio) and atr_ratio > c.abnormal_atr_multiple:
            reasons.append(f"ATR {atr_ratio:.1f}x its median — abnormal volatility")
            return RegimeResult(
                Regime.ABNORMAL,
                VolRegime.EXTREME,
                adx_now,
                p_di,
                m_di,
                atr_now,
                atr_pct,
                atr_ratio,
                norm_slope,
                tuple(reasons),
            )
        if spread_median > 0 and spread_points > spread_median * c.abnormal_spread_multiple:
            reasons.append(
                f"spread {spread_points:.0f}pts is {spread_points / spread_median:.1f}x median"
            )
            return RegimeResult(
                Regime.ABNORMAL,
                vol_regime,
                adx_now,
                p_di,
                m_di,
                atr_now,
                atr_pct,
                atr_ratio,
                norm_slope,
                tuple(reasons),
            )

        # --- trend vs range ----------------------------------------------------------
        bullish = p_di > m_di
        if adx_now >= c.strong_trend_adx:
            regime = Regime.STRONG_BULL if bullish else Regime.STRONG_BEAR
            reasons.append(f"ADX {adx_now:.1f} >= {c.strong_trend_adx} strong trend")
        elif adx_now >= c.moderate_trend_adx:
            regime = Regime.MODERATE_BULL if bullish else Regime.MODERATE_BEAR
            reasons.append(f"ADX {adx_now:.1f} >= {c.moderate_trend_adx} moderate trend")
        else:
            regime = Regime.RANGE
            reasons.append(f"ADX {adx_now:.1f} below {c.moderate_trend_adx} — ranging")

        # A trend label that disagrees with the EMA slope is downgraded rather than
        # trusted: ADX measures directional strength, not which way.
        if regime in (Regime.STRONG_BULL, Regime.MODERATE_BULL) and norm_slope < -0.02:
            regime = Regime.RANGE
            reasons.append("ADX says bull but EMA50 slope is negative — downgraded to RANGE")
        elif regime in (Regime.STRONG_BEAR, Regime.MODERATE_BEAR) and norm_slope > 0.02:
            regime = Regime.RANGE
            reasons.append("ADX says bear but EMA50 slope is positive — downgraded to RANGE")

        reasons.append(f"volatility {vol_regime} (ATR pct {atr_pct:.2f})")
        return RegimeResult(
            regime,
            vol_regime,
            adx_now,
            p_di,
            m_di,
            atr_now,
            atr_pct,
            atr_ratio,
            norm_slope,
            tuple(reasons),
        )

    def volatility_state(
        self,
        by_tf: dict[str, BarSeries],
        spread_points: float,
        spread_median: float,
    ) -> VolatilityState:
        def a(name: str) -> float:
            s = by_tf.get(name)
            return atr_last(s, 14) if s is not None and len(s) > 15 else float("nan")

        h1 = by_tf.get("H1")
        pct = float("nan")
        if h1 is not None and len(h1) > 60:
            arr = atr(h1, 14)
            finite = arr[np.isfinite(arr)]
            if finite.size > 30:
                pct = last_valid(percentile_rank(finite, min(100, finite.size)), float("nan"))
        rv = float("nan")
        if h1 is not None and len(h1) > 30:
            rv = last_valid(realized_volatility(h1.close, 20), float("nan"))

        return VolatilityState(
            atr_d1=a("D1"),
            atr_h4=a("H4"),
            atr_h1=a("H1"),
            atr_m15=a("M15"),
            atr_m5=a("M5"),
            atr_h1_percentile=pct,
            realized_vol=rv,
            vol_regime=self.volatility_regime(pct),
            spread_points=spread_points,
            spread_median_points=spread_median if spread_median > 0 else 25.0,
        )
