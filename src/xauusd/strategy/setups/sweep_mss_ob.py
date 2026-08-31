"""Strategy: sweep -> MSS -> retrace into the ORDER BLOCK (breaker as fallback).

Same narrative as sweep_mss_fvg, different entry zone. Kept as a separate strategy
rather than a branch so that each is validated, gated and reported independently —
if only one of the two clears the Phase 10 gate, only that one trades.
"""

from __future__ import annotations

from datetime import timedelta

from xauusd.config.settings import Settings
from xauusd.core.order_blocks import OrderBlockEngine
from xauusd.core.support_resistance import SREngine, is_correct_side
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Direction, OrderBlockKind, Regime, Session, Timeframe
from xauusd.domain.types import MarketSnapshot, OrderBlock, TradePlan
from xauusd.strategy.base import StrategyMeta, build_targets, structural_stop

SETUP_TF = Timeframe.M15


class SweepMssOb:
    meta = StrategyMeta(
        name="sweep_mss_ob",
        version="1.0",
        allowed_regimes=frozenset(
            {
                Regime.STRONG_BULL,
                Regime.MODERATE_BULL,
                Regime.RANGE,
                Regime.MODERATE_BEAR,
                Regime.STRONG_BEAR,
            }
        ),
        allowed_sessions=frozenset({Session.LONDON, Session.NEW_YORK, Session.OVERLAP}),
        description="HTF-aligned sweep, MSS, retrace into the originating order block",
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.ob_engine = OrderBlockEngine(self.settings.order_block)
        self.sr_engine = SREngine(self.settings.sr)
        self.max_sweep_age_bars = 15
        self.min_sweep_quality = 0.45
        self.max_entry_distance_atr = 4.0

    def detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]:
        atr = snap.volatility.atr_m15
        if not atr or atr != atr or atr <= 0:
            return []
        out: list[TradePlan] = []
        for d in (Direction.LONG, Direction.SHORT):
            p = self._for_direction(snap, d, atr)
            if p:
                out.append(p)
        return out

    def _for_direction(
        self, snap: MarketSnapshot, direction: Direction, atr: float
    ) -> TradePlan | None:
        t = self.settings.thresholds

        # H4 is included deliberately: the classifier's htf_conflict check spans
        # MN/W/D/H4, so omitting H4 here would emit candidates that are guaranteed to
        # be rejected downstream, wasting work and polluting the rejection ledger.
        for tf in (Timeframe.H4, Timeframe.D1, Timeframe.W1, Timeframe.MN1):
            if snap.bias(tf).conflicts_with(direction):
                return None

        cutoff = snap.ts - timedelta(seconds=SETUP_TF.seconds * self.max_sweep_age_bars)
        sweeps = [
            s
            for s in snap.sweeps
            if s.direction is direction and s.ts >= cutoff and s.quality >= self.min_sweep_quality
        ]
        if not sweeps:
            return None
        sweep = max(sweeps, key=lambda s: s.quality)
        if sweep.displacement_after_atr < self.settings.liquidity.displacement_after_sweep_atr:
            return None

        st = snap.structures.get(SETUP_TF)
        mss = st.last_mss if st else None
        if mss is None or mss.direction is not direction or mss.ts < sweep.ts:
            return None

        ob = self._entry_block(snap, direction, atr)
        if ob is None:
            return None

        # Enter at the near edge of the block for a tighter stop, midpoint if it is wide.
        entry = (
            ob.midpoint
            if ob.height > 0.8 * atr
            else (ob.top if direction is Direction.LONG else ob.bottom)
        )
        if abs(entry - snap.quote.mid) > self.max_entry_distance_atr * atr:
            return None
        if not is_correct_side(snap.dealing_range, entry, direction, tolerance=0.10):
            return None

        sweep_extreme = (
            sweep.pool.price + sweep.penetration
            if direction is Direction.SHORT
            else sweep.pool.price - sweep.penetration
        )
        stop = structural_stop(direction, ob.top, ob.bottom, sweep_extreme, atr, buffer_atr=0.15)
        if (direction is Direction.LONG and stop >= entry) or (
            direction is Direction.SHORT and stop <= entry
        ):
            return None

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
            return None

        return TradePlan(
            strategy=self.meta.name,
            strategy_version=self.meta.version,
            direction=direction,
            entry=entry,
            stop_loss=stop,
            targets=tuple(targets),
            ts=snap.ts,
            setup_timeframe=SETUP_TF,
            entry_zone_top=ob.top,
            entry_zone_bottom=ob.bottom,
            invalidation=(f"a {SETUP_TF} body close beyond {stop:.2f} invalidates the order block"),
            evidence={
                "sweep_kind": str(sweep.pool.kind),
                "sweep_price": sweep.pool.price,
                "sweep_quality": sweep.quality,
                "mss_displacement_atr": mss.displacement_atr,
                "ob_kind": str(ob.kind),
                "ob_top": ob.top,
                "ob_bottom": ob.bottom,
                "ob_state": str(ob.state),
                "ob_quality": self.ob_engine.score(
                    ob, snap.dealing_range, swept_liquidity=True, atr_value=atr
                ),
                "chain": "sweep -> displacement -> MSS -> order block retrace",
            },
        )

    def _entry_block(
        self, snap: MarketSnapshot, direction: Direction, atr: float
    ) -> OrderBlock | None:
        blocks = [
            o
            for o in snap.order_blocks
            if o.direction is direction and o.is_tradable and o.caused_bos
        ]
        if not blocks:
            # Breakers are the fallback: a failed block that trapped participants.
            blocks = [
                o
                for o in snap.order_blocks
                if o.direction is direction
                and o.is_tradable
                and o.kind in (OrderBlockKind.BULL_BREAKER, OrderBlockKind.BEAR_BREAKER)
            ]
        if not blocks:
            return None
        price = snap.quote.mid
        blocks.sort(key=lambda o: (-o.displacement_atr, abs(o.midpoint - price)))
        return blocks[0]
