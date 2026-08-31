"""Strategy: liquidity sweep -> displacement -> MSS -> retrace into the FVG.

This is the primary setup and it encodes the confluence chain from the brief literally:

    LIQUIDITY SWEEP
  + STRONG REJECTION / DISPLACEMENT
  + MARKET STRUCTURE SHIFT
  + HIGHER-TIMEFRAME CONTEXT
  + HIGH-QUALITY ENTRY ZONE
  + VALID RR
  + NO NEWS CONFLICT
  = TRADE CANDIDATE

Each link is a hard requirement here, so the strategy simply cannot emit a candidate
from a sweep alone. What it emits is still only a CANDIDATE: scoring, gating,
classification and risk all run afterwards and any of them can reject it.
"""

from __future__ import annotations

from datetime import timedelta

from xauusd.config.settings import Settings
from xauusd.core.fair_value_gap import FVGEngine
from xauusd.core.support_resistance import SREngine, is_correct_side
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Direction, Regime, Session, Timeframe
from xauusd.domain.types import FVG, MarketSnapshot, Sweep, TradePlan
from xauusd.strategy.base import StrategyMeta, build_targets, structural_stop

SETUP_TF = Timeframe.M15


class SweepMssFvg:
    meta = StrategyMeta(
        name="sweep_mss_fvg",
        version="1.0",
        # Validated regimes and sessions are set from the Phase 10 report. These are the
        # DEV defaults; StrategyStatus in the database is what gates live routing.
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
        description="HTF-aligned sweep, displacement, MSS, retrace into the displacement FVG",
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.fvg_engine = FVGEngine(self.settings.fvg)
        self.sr_engine = SREngine(self.settings.sr)
        # A sweep older than this is history, not the reason price is where it is.
        self.max_sweep_age_bars = 15
        self.min_sweep_quality = 0.45
        self.max_entry_distance_atr = 4.0

    def detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]:
        atr = snap.volatility.atr_m15
        if not atr or atr != atr or atr <= 0:
            return []

        plans: list[TradePlan] = []
        for direction in (Direction.LONG, Direction.SHORT):
            plan = self._for_direction(view, snap, direction, atr)
            if plan is not None:
                plans.append(plan)
        return plans

    # -- the confluence chain ----------------------------------------------------------

    def _for_direction(
        self, view: MarketView, snap: MarketSnapshot, direction: Direction, atr: float
    ) -> TradePlan | None:
        t = self.settings.thresholds

        # LINK 1 — higher-timeframe context. A daily-or-above conflict ends it here.
        # H4 is included deliberately: the classifier's htf_conflict check spans
        # MN/W/D/H4, so omitting H4 here would emit candidates that are guaranteed to
        # be rejected downstream, wasting work and polluting the rejection ledger.
        for tf in (Timeframe.H4, Timeframe.D1, Timeframe.W1, Timeframe.MN1):
            if snap.bias(tf).conflicts_with(direction):
                return None

        # LINK 2 — a recent, high-quality liquidity sweep in this direction.
        sweep = self._recent_sweep(snap, direction)
        if sweep is None:
            return None

        # LINK 3 — displacement away from the swept level.
        if sweep.displacement_after_atr < self.settings.liquidity.displacement_after_sweep_atr:
            return None

        # LINK 4 — a market structure shift confirming the reversal, AFTER the sweep.
        st = snap.structures.get(SETUP_TF)
        mss = st.last_mss if st else None
        if mss is None or mss.direction is not direction:
            return None
        if mss.ts < sweep.ts:
            return None  # the shift must follow the sweep, not precede it

        # LINK 5 — an unmitigated FVG from the displacement leg, as the entry zone.
        fvg = self._entry_fvg(snap, direction, sweep, atr)
        if fvg is None:
            return None

        entry = self.fvg_engine.entry_price(fvg)
        price = snap.quote.mid
        if abs(entry - price) > self.max_entry_distance_atr * atr:
            return None  # too far away to be a realistic fill

        # LINK 6 — correct side of the dealing range.
        if not is_correct_side(snap.dealing_range, entry, direction, tolerance=0.10):
            return None

        # Structural stop: beyond the extreme of the sweep that created the setup.
        sweep_extreme = self._sweep_extreme(sweep, direction)
        stop = structural_stop(
            direction=direction,
            zone_top=fvg.top,
            zone_bottom=fvg.bottom,
            sweep_extreme=sweep_extreme,
            atr_value=atr,
            buffer_atr=0.15,
        )
        if (direction is Direction.LONG and stop >= entry) or (
            direction is Direction.SHORT and stop <= entry
        ):
            return None

        # LINK 7 — targets anchored to real opposing liquidity, and a valid RR.
        targets = build_targets(
            entry=entry,
            stop_loss=stop,
            direction=direction,
            pools=list(snap.liquidity),
            sr_levels=list(snap.sr_levels),
            min_rr=t.min_rr,
            preferred_rr=t.preferred_rr,
            sr_engine=self.sr_engine,
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
            entry_zone_top=fvg.top,
            entry_zone_bottom=fvg.bottom,
            invalidation=(
                f"a {SETUP_TF} close beyond {stop:.2f} invalidates the shift; "
                f"the premise is that the sweep of {sweep.pool.kind} at "
                f"{sweep.pool.price:.2f} was the reversal"
            ),
            evidence={
                "sweep_kind": str(sweep.pool.kind),
                "sweep_price": sweep.pool.price,
                "sweep_quality": sweep.quality,
                "sweep_displacement_atr": sweep.displacement_after_atr,
                "mss_displacement_atr": mss.displacement_atr,
                "fvg_top": fvg.top,
                "fvg_bottom": fvg.bottom,
                "fvg_state": str(fvg.state),
                "fvg_quality": self.fvg_engine.score(
                    fvg,
                    snap.dealing_range,
                    swept_liquidity=True,
                    htf_aligned=not snap.bias(Timeframe.H4).conflicts_with(direction),
                ),
                "chain": "sweep -> displacement -> MSS -> FVG retrace",
            },
        )

    # -- helpers -----------------------------------------------------------------------

    def _recent_sweep(self, snap: MarketSnapshot, direction: Direction):  # type: ignore[no-untyped-def]
        cutoff = snap.ts - timedelta(seconds=SETUP_TF.seconds * self.max_sweep_age_bars)
        candidates = [
            s
            for s in snap.sweeps
            if s.direction is direction and s.ts >= cutoff and s.quality >= self.min_sweep_quality
        ]
        return max(candidates, key=lambda s: s.quality) if candidates else None

    @staticmethod
    def _sweep_extreme(sweep: Sweep, direction: Direction) -> float:
        """The price the sweep reached — where the stop belongs, plus a buffer."""
        if direction is Direction.SHORT:
            return sweep.pool.price + sweep.penetration
        return sweep.pool.price - sweep.penetration

    def _entry_fvg(
        self, snap: MarketSnapshot, direction: Direction, sweep: Sweep, atr: float
    ) -> FVG | None:
        """The best unfilled gap created by the displacement, after the sweep."""
        candidates = [
            f
            for f in snap.fvgs
            if f.direction is direction
            and f.is_tradable
            and f.formed_ts >= sweep.ts
            and f.displacement_atr >= self.settings.fvg.min_displacement_atr
        ]
        if not candidates:
            return None
        # Prefer displacement, then proximity to current price.
        price = snap.quote.mid
        candidates.sort(key=lambda f: (-f.displacement_atr, abs(f.midpoint - price)))
        return candidates[0]
