"""Market analyzer: turns a MarketView into one MarketSnapshot.

Every engine is invoked here, in dependency order, and the result is a single immutable
object that the strategy, scoring and gate layers read. Nothing downstream touches a
BarSeries directly, so nothing downstream can accidentally reach past `view.now`.

Higher-timeframe analysis is cached on the bar that produced it: a D1 read does not
change until a new D1 bar closes, so the M5 decision cycle stays inside its budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from xauusd.config.settings import Settings
from xauusd.core.fair_value_gap import FVGEngine
from xauusd.core.indicators import atr_last
from xauusd.core.liquidity import LiquidityEngine
from xauusd.core.order_blocks import OrderBlockEngine
from xauusd.core.regime import RegimeEngine
from xauusd.core.sessions import SessionEngine
from xauusd.core.structure import StructureEngine, detect_swings
from xauusd.core.support_resistance import SREngine
from xauusd.data.marketview import MarketView
from xauusd.data.series import BarSeries
from xauusd.domain.enums import (
    Bias,
    LiquidityKind,
    MacroBias,
    NewsRisk,
    Session,
    Timeframe,
)
from xauusd.domain.types import (
    FVG,
    DealingRange,
    LiquidityPool,
    MacroState,
    MarketSnapshot,
    NewsState,
    OrderBlock,
    Quote,
    SRLevel,
    Sweep,
    TimeframeStructure,
)

HTF = (Timeframe.MN1, Timeframe.W1, Timeframe.D1, Timeframe.H4)
LTF = (Timeframe.H1, Timeframe.M15, Timeframe.M5, Timeframe.M1)

# Pruning. Analysis of the whole history produces hundreds of pools and dozens of
# stale sweeps, which is noise, not information: a sweep 400 bars ago is history, and a
# liquidity pool 20 ATR away is not a target for this trade. These bounds keep the
# snapshot to what is actually actionable now.
MAX_POOL_DISTANCE_ATR = 12.0
MAX_POOLS = 40
SWEEP_RECENCY_BARS = 30
MAX_ZONE_DISTANCE_ATR = 10.0
MAX_ZONES = 30

UNKNOWN_MACRO = MacroState(
    bias=MacroBias.UNKNOWN,
    dxy_level=None,
    dxy_change_1d=None,
    dxy_change_5d=None,
    dxy_trend=Bias.NEUTRAL,
    us10y=None,
    us2y=None,
    real10y=None,
    real10y_change_5d=None,
    breakeven10y=None,
    yields_trend=Bias.NEUTRAL,
    curve_10y2y=None,
    as_of=None,
    is_stale=True,
)

# Absence of news data is treated as MODERATE, never LOW. Quiet feeds are not quiet
# markets, and defaulting to LOW would let a broken feed unlock A+ classification.
UNKNOWN_NEWS = NewsState(
    risk=NewsRisk.MODERATE,
    blackout=False,
    blackout_reason=None,
    blackout_until=None,
    next_event_name=None,
    next_event_ts=None,
    minutes_to_next_event=None,
    directional_hint=Bias.NEUTRAL,
    drivers=("news feed unavailable",),
    is_stale=True,
)


@dataclass(slots=True)
class _CacheEntry:
    bar_ts: int
    value: object


class MarketAnalyzer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        s = self.settings
        self.structure = StructureEngine(s.structure)
        self.liquidity = LiquidityEngine(s.liquidity)
        self.fvg = FVGEngine(s.fvg)
        self.order_blocks = OrderBlockEngine(s.order_block)
        self.sr = SREngine(s.sr)
        self.sessions = SessionEngine(s.session)
        self.regime = RegimeEngine(s.regime)
        self._cache: dict[str, _CacheEntry] = {}

    # -- caching -----------------------------------------------------------------------

    def _cached(self, key: str, series: BarSeries, compute):  # type: ignore[no-untyped-def]
        """Recompute only when a new bar has closed on that timeframe."""
        if not len(series):
            return compute()
        bar_ts = int(series.ts[-1])
        hit = self._cache.get(key)
        if hit is not None and hit.bar_ts == bar_ts:
            return hit.value
        value = compute()
        self._cache[key] = _CacheEntry(bar_ts, value)
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- pruning -----------------------------------------------------------------------

    @staticmethod
    def _prune_pools(
        pools: list[LiquidityPool], price: float, atr_value: float
    ) -> list[LiquidityPool]:
        """Keep pools that could plausibly matter to a trade placed now.

        Period levels (PDH/PDL/PWH/PWL) are always kept regardless of distance: they
        are reference draws even when far away, and dropping them would silently
        remove the best take-profit anchors.
        """
        if atr_value <= 0 or not np.isfinite(atr_value):
            return pools[:MAX_POOLS]
        limit = MAX_POOL_DISTANCE_ATR * atr_value
        always = {
            LiquidityKind.PDH,
            LiquidityKind.PDL,
            LiquidityKind.PWH,
            LiquidityKind.PWL,
        }
        near = [p for p in pools if p.kind in always or abs(p.price - price) <= limit]
        near.sort(
            key=lambda p: (
                p.kind not in always,
                not p.is_resting,
                abs(p.price - price) / atr_value - p.strength,
            )
        )
        return near[:MAX_POOLS]

    @staticmethod
    def _prune_sweeps(sweeps: list[Sweep], series: BarSeries) -> list[Sweep]:
        """Only sweeps recent enough to still be the reason price is where it is."""
        if not len(series) or not sweeps:
            return []
        cutoff_idx = max(0, len(series) - SWEEP_RECENCY_BARS)
        cutoff = datetime.fromtimestamp(int(series.ts[cutoff_idx]), UTC)
        return [s for s in sweeps if s.ts >= cutoff]

    @staticmethod
    def _prune_zones(zones: list, price: float, atr_value: float) -> list:
        """Drop zones too far away to be reached, keeping the closest tradable ones."""
        if atr_value <= 0 or not np.isfinite(atr_value):
            return zones[:MAX_ZONES]
        limit = MAX_ZONE_DISTANCE_ATR * atr_value
        near = [z for z in zones if abs(z.midpoint - price) <= limit]
        near.sort(key=lambda z: (not z.is_tradable, abs(z.midpoint - price)))
        return near[:MAX_ZONES]

    # -- components --------------------------------------------------------------------

    def structures(self, view: MarketView) -> dict[Timeframe, TimeframeStructure]:
        out: dict[Timeframe, TimeframeStructure] = {}
        for tf in self.settings.data.analysis_timeframes:
            n = self.settings.data.bars_to_load.get(str(tf), 300)
            series = view.bars(tf, n)
            if not len(series):
                continue
            out[tf] = self._cached(
                f"struct:{tf}", series, lambda s=series: self.structure.analyze(s)
            )
        return out

    def asia_range(self, view: MarketView) -> tuple[float, float, datetime] | None:
        """The Asian session high/low — the reference liquidity for the London open."""
        start, end = self.sessions.session_bounds(view.now, Session.ASIA)
        bars = view.bars_between(Timeframe.M15, start, min(end, view.now))
        if not len(bars):
            return None
        return float(np.max(bars.high)), float(np.min(bars.low)), start

    def session_state(self, view: MarketView):  # type: ignore[no-untyped-def]
        asia = self.asia_range(view)
        session = self.sessions.session_for(view.now)
        s_high = s_low = None
        if session is not Session.OFF:
            s_start, _ = self.sessions.session_bounds(view.now, session)
            sb = view.bars_between(Timeframe.M15, s_start, view.now)
            if len(sb):
                s_high, s_low = float(np.max(sb.high)), float(np.min(sb.low))
        return self.sessions.state(
            view.now,
            asia_high=asia[0] if asia else None,
            asia_low=asia[1] if asia else None,
            session_high=s_high,
            session_low=s_low,
        )

    def _compute_zones(
        self,
        view: MarketView,
        setup_series: BarSeries,
        atr_setup: float,
        h1: BarSeries,
        d1: BarSeries,
        w1: BarSeries,
    ) -> tuple[list, list, list, list]:
        """All zone analysis for one setup bar. Cached by the caller."""
        s = self.settings
        swings = detect_swings(
            setup_series, s.structure.swing_lookback, s.structure.swing_min_atr, atr_setup
        )
        asia = self.asia_range(view)
        pools, sweeps = self.liquidity.analyze(setup_series, swings, d1, w1, asia)
        fvgs = self.fvg.detect(setup_series, atr_setup)
        events = self.structure.detect_events(setup_series, swings, atr_setup)
        obs = self.order_blocks.detect(setup_series, events, atr_setup, fvgs)
        obs += self.order_blocks.detect_breakers(setup_series, obs)

        # H1 zones as well: higher-timeframe FVGs and order blocks carry more weight.
        if len(h1) > 60:
            atr_h1 = atr_last(h1, 14)
            if np.isfinite(atr_h1) and atr_h1 > 0:
                fvgs += self.fvg.detect(h1, atr_h1)
                h1_swings = detect_swings(
                    h1, s.structure.swing_lookback, s.structure.swing_min_atr, atr_h1
                )
                h1_events = self.structure.detect_events(h1, h1_swings, atr_h1)
                obs += self.order_blocks.detect(h1, h1_events, atr_h1, fvgs)
        return pools, sweeps, fvgs, obs

    # -- top level ---------------------------------------------------------------------

    def analyze(
        self,
        view: MarketView,
        macro: MacroState | None = None,
        news: NewsState | None = None,
        spread_points: float = 0.0,
        spread_median: float = 25.0,
    ) -> MarketSnapshot:
        s = self.settings
        structures = self.structures(view)

        m15 = view.bars(Timeframe.M15, s.data.bars_to_load.get("M15", 1000))
        m5 = view.bars(Timeframe.M5, s.data.bars_to_load.get("M5", 1500))
        h1 = view.bars(Timeframe.H1, s.data.bars_to_load.get("H1", 800))
        h4 = view.bars(Timeframe.H4, s.data.bars_to_load.get("H4", 500))
        d1 = view.bars(Timeframe.D1, s.data.bars_to_load.get("D1", 400))
        w1 = view.bars(Timeframe.W1, s.data.bars_to_load.get("W1", 200))

        # Setup timeframe for zones is M15; execution refinement happens on M5.
        setup_series = m15 if len(m15) > 60 else h1
        atr_setup = atr_last(setup_series, 14) if len(setup_series) > 20 else float("nan")

        pools: list[LiquidityPool] = []
        sweeps: list[Sweep] = []
        fvgs: list[FVG] = []
        obs: list[OrderBlock] = []

        if len(setup_series) > 40 and np.isfinite(atr_setup) and atr_setup > 0:
            # Zones derive from the SETUP timeframe, which changes only when a setup
            # bar closes. Caching on that bar means the three M5 evaluations inside one
            # M15 bar do the work once, not three times.
            pools, sweeps, fvgs, obs = self._cached(
                "zones",
                setup_series,
                lambda: self._compute_zones(view, setup_series, atr_setup, h1, d1, w1),
            )

        price_now = view.price()
        price_now = view.price()
        if np.isfinite(atr_setup) and atr_setup > 0:
            pools = self._prune_pools(pools, price_now, atr_setup)
            sweeps = self._prune_sweeps(sweeps, setup_series)
            fvgs = self._prune_zones(fvgs, price_now, atr_setup)
            obs = self._prune_zones(obs, price_now, atr_setup)

        sr_levels: list[SRLevel] = self._cached(
            "sr",
            d1 if len(d1) else h4,
            lambda: self.sr.build(
                {Timeframe.D1: d1, Timeframe.H4: h4, Timeframe.H1: h1, Timeframe.W1: w1}
            ),
        )

        regime_series = h1 if len(h1) > 100 else h4
        reg = self.regime.classify(regime_series, spread_points, spread_median)
        vol = self.regime.volatility_state(
            {"D1": d1, "H4": h4, "H1": h1, "M15": m15, "M5": m5},
            spread_points,
            spread_median,
        )

        # The dealing range comes from H4 when available: it is the range that defines
        # premium/discount for a swing setup, not the M15 noise range.
        dr: DealingRange | None = None
        for tf in (Timeframe.H4, Timeframe.H1, Timeframe.M15):
            st = structures.get(tf)
            if st and st.dealing_range:
                dr = st.dealing_range
                break

        quote = view.quote or Quote(view.now, view.price(), view.price())

        return MarketSnapshot(
            ts=view.now,
            symbol=view.symbol,
            quote=quote,
            structures=structures,
            liquidity=tuple(pools),
            sweeps=tuple(sweeps),
            fvgs=tuple(fvgs),
            order_blocks=tuple(obs),
            sr_levels=tuple(sr_levels),
            dealing_range=dr,
            session=self.session_state(view),
            volatility=vol,
            regime=reg.regime,
            macro=macro or UNKNOWN_MACRO,
            news=news or UNKNOWN_NEWS,
        )


def snapshot_payload(snap: MarketSnapshot) -> dict:
    """Serialise a snapshot for the database and the dashboard."""
    return {
        "ts": snap.ts.isoformat(),
        "symbol": snap.symbol,
        "price": snap.quote.mid,
        "spread_points": snap.volatility.spread_points,
        "regime": str(snap.regime),
        "vol_regime": str(snap.volatility.vol_regime),
        "session": str(snap.session.session),
        "killzone": str(snap.session.killzone),
        "htf_bias": str(snap.htf_bias),
        "biases": {str(tf): str(st.bias) for tf, st in snap.structures.items()},
        "atr": {
            "d1": snap.volatility.atr_d1,
            "h4": snap.volatility.atr_h4,
            "h1": snap.volatility.atr_h1,
            "m15": snap.volatility.atr_m15,
        },
        "dealing_range": (
            {
                "high": snap.dealing_range.high,
                "low": snap.dealing_range.low,
                "equilibrium": snap.dealing_range.equilibrium,
                "position": snap.dealing_range.position_of(snap.quote.mid),
                "zone": snap.dealing_range.zone_label(snap.quote.mid),
            }
            if snap.dealing_range
            else None
        ),
        "liquidity": [
            {
                "kind": str(p.kind),
                "price": p.price,
                "resting": p.is_resting,
                "touches": p.touches,
                "strength": p.strength,
            }
            for p in snap.liquidity
        ],
        "sweeps": [
            {
                "ts": s.ts.isoformat(),
                "kind": str(s.pool.kind),
                "price": s.pool.price,
                "direction": str(s.direction),
                "quality": s.quality,
                "displacement_atr": s.displacement_after_atr,
            }
            for s in snap.sweeps
        ],
        "fvgs": [
            {
                "direction": str(f.direction),
                "top": f.top,
                "bottom": f.bottom,
                "state": str(f.state),
                "size_atr": f.size_atr,
                "displacement_atr": f.displacement_atr,
                "tf": str(f.timeframe),
            }
            for f in snap.fvgs
        ],
        "order_blocks": [
            {
                "kind": str(o.kind),
                "top": o.top,
                "bottom": o.bottom,
                "state": str(o.state),
                "displacement_atr": o.displacement_atr,
                "tf": str(o.timeframe),
            }
            for o in snap.order_blocks
        ],
        "sr_levels": [
            {
                "kind": str(lvl.kind),
                "price": lvl.price,
                "touches": lvl.touches,
                "importance": lvl.importance,
                "tf": str(lvl.timeframe),
            }
            for lvl in snap.sr_levels
        ],
        "macro": {
            "bias": str(snap.macro.bias),
            "dxy": snap.macro.dxy_level,
            "us10y": snap.macro.us10y,
            "real10y": snap.macro.real10y,
            "stale": snap.macro.is_stale,
        },
        "news": {
            "risk": str(snap.news.risk),
            "blackout": snap.news.blackout,
            "reason": snap.news.blackout_reason,
            "next_event": snap.news.next_event_name,
            "minutes_to_event": snap.news.minutes_to_next_event,
        },
    }
