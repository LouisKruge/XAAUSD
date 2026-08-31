"""Strategy: previous-day high/low sweep and rejection, in a validated RANGE regime.

Deliberately narrow. Mean reversion from a daily level only makes sense when the market
is actually ranging; in a trend the same pattern is a continuation entry against the
trend and loses. So the regime whitelist is RANGE only, and that is enforced by the
registry rather than left to judgement.
"""

from __future__ import annotations

from datetime import timedelta

from xauusd.config.settings import Settings
from xauusd.core.support_resistance import SREngine
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Direction, LiquidityKind, Regime, Session, Timeframe
from xauusd.domain.types import MarketSnapshot, TradePlan
from xauusd.strategy.base import StrategyMeta, build_targets, structural_stop

SETUP_TF = Timeframe.M15
PERIOD_KINDS = {
    LiquidityKind.PDH,
    LiquidityKind.PDL,
    LiquidityKind.PWH,
    LiquidityKind.PWL,
}


class PdhPdlReversion:
    meta = StrategyMeta(
        name="pdh_pdl_reversion",
        version="1.0",
        # RANGE only. In a trend this same pattern is a counter-trend entry.
        allowed_regimes=frozenset({Regime.RANGE}),
        allowed_sessions=frozenset({Session.LONDON, Session.NEW_YORK, Session.OVERLAP}),
        description="Previous day/week level sweep and rejection, range regime only",
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.sr_engine = SREngine(self.settings.sr)
        self.max_sweep_age_bars = 8
        self.min_sweep_quality = 0.55  # higher bar: mean reversion needs a clean rejection

    def detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]:
        atr = snap.volatility.atr_m15
        if not atr or atr != atr or atr <= 0:
            return []
        if snap.regime is not Regime.RANGE:
            return []

        t = self.settings.thresholds
        cutoff = snap.ts - timedelta(seconds=SETUP_TF.seconds * self.max_sweep_age_bars)
        out: list[TradePlan] = []

        for sweep in snap.sweeps:
            if sweep.pool.kind not in PERIOD_KINDS:
                continue
            if sweep.ts < cutoff or sweep.quality < self.min_sweep_quality:
                continue
            if not sweep.closed_back_inside:
                continue
            direction = sweep.direction

            st = snap.structures.get(SETUP_TF)
            if not st or not st.last_event or st.last_event.direction is not direction:
                continue

            entry = snap.quote.mid
            extreme = (
                sweep.pool.price + sweep.penetration
                if direction is Direction.SHORT
                else sweep.pool.price - sweep.penetration
            )
            stop = structural_stop(direction, extreme, extreme, extreme, atr, buffer_atr=0.20)
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
                        f"acceptance beyond {sweep.pool.kind} at {sweep.pool.price:.2f} "
                        f"means this is a breakout, not a sweep"
                    ),
                    evidence={
                        "level_kind": str(sweep.pool.kind),
                        "level_price": sweep.pool.price,
                        "sweep_quality": sweep.quality,
                        "regime": str(snap.regime),
                        "chain": "period level sweep -> rejection -> reversion (range only)",
                    },
                )
            )
        return out[:1]  # at most one reversion candidate per evaluation
