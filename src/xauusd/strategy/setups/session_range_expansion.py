"""Strategy: Asian-range sweep at the London open, continuation into the HTF draw.

The Asian session builds a compressed range whose extremes accumulate stops. London
frequently takes one side and then expands in the other direction toward the higher
timeframe draw on liquidity. This strategy trades only that specific sequence and only
in the London window.
"""

from __future__ import annotations

from xauusd.config.settings import Settings
from xauusd.core.support_resistance import SREngine, is_correct_side
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Direction, Regime, Session, Timeframe
from xauusd.domain.types import MarketSnapshot, TradePlan
from xauusd.strategy.base import StrategyMeta, build_targets, structural_stop

SETUP_TF = Timeframe.M15


class SessionRangeExpansion:
    meta = StrategyMeta(
        name="session_range_expansion",
        version="1.0",
        allowed_regimes=frozenset(
            {Regime.STRONG_BULL, Regime.MODERATE_BULL, Regime.MODERATE_BEAR, Regime.STRONG_BEAR}
        ),
        # London and the overlap only. The premise is specifically about the London open.
        allowed_sessions=frozenset({Session.LONDON, Session.OVERLAP}),
        description="Asian range sweep at the London open, expansion toward the HTF draw",
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.sr_engine = SREngine(self.settings.sr)
        self.max_minutes_into_london = 240
        self.min_range_atr = 0.8
        self.max_range_atr = 4.0

    def detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]:
        atr = snap.volatility.atr_m15
        if not atr or atr != atr or atr <= 0:
            return []
        s = snap.session
        if s.session not in self.meta.allowed_sessions:
            return None or []
        if s.minutes_into_session > self.max_minutes_into_london:
            return []
        if s.asia_high is None or s.asia_low is None:
            return []

        asia_range = s.asia_high - s.asia_low
        # Too tight and the sweep is noise; too wide and there was no compression to expand from.
        if not (self.min_range_atr * atr <= asia_range <= self.max_range_atr * atr):
            return []

        price = snap.quote.mid
        t = self.settings.thresholds
        out: list[TradePlan] = []

        # Which side of the Asian range was taken, and did price reject back inside?
        swept_high = price < s.asia_high and any(
            sw.direction is Direction.SHORT and abs(sw.pool.price - s.asia_high) <= 0.5 * atr
            for sw in snap.sweeps
        )
        swept_low = price > s.asia_low and any(
            sw.direction is Direction.LONG and abs(sw.pool.price - s.asia_low) <= 0.5 * atr
            for sw in snap.sweeps
        )

        for direction, taken, extreme in (
            (Direction.SHORT, swept_high, s.asia_high),
            (Direction.LONG, swept_low, s.asia_low),
        ):
            if not taken:
                continue
            # The expansion must run WITH the higher-timeframe draw, not against it.
            if snap.bias(Timeframe.H4).conflicts_with(direction):
                continue
            if snap.bias(Timeframe.D1).conflicts_with(direction):
                continue

            st = snap.structures.get(SETUP_TF)
            if not st or not (st.last_mss or st.last_bos):
                continue
            event = st.last_mss or st.last_bos
            if event.direction is not direction:
                continue

            entry = price
            if not is_correct_side(snap.dealing_range, entry, direction, tolerance=0.15):
                continue
            stop = structural_stop(
                direction,
                zone_top=extreme,
                zone_bottom=extreme,
                sweep_extreme=extreme,
                atr_value=atr,
                buffer_atr=0.25,
            )
            if (direction is Direction.LONG and stop >= entry) or (
                direction is Direction.SHORT and stop <= entry
            ):
                continue
            targets = build_targets(
                entry,
                stop,
                direction,
                list(snap.liquidity),
                list(snap.sr_levels),
                t.min_rr,
                t.preferred_rr,
                self.sr_engine,
            )
            if not targets:
                continue
            out.append(
                TradePlan(
                    strategy=self.meta.name,
                    strategy_version=self.meta.version,
                    direction=direction,
                    entry=entry,
                    stop_loss=stop,
                    targets=tuple(targets),
                    ts=snap.ts,
                    setup_timeframe=SETUP_TF,
                    invalidation=(
                        f"a return beyond the Asian {'high' if direction is Direction.SHORT else 'low'} "
                        f"at {extreme:.2f} means the expansion failed"
                    ),
                    evidence={
                        "asia_high": s.asia_high,
                        "asia_low": s.asia_low,
                        "asia_range_atr": asia_range / atr,
                        "minutes_into_session": s.minutes_into_session,
                        "structure_event": str(event.kind),
                        "chain": "Asian range sweep -> structure -> expansion to HTF draw",
                    },
                )
            )
        return out
