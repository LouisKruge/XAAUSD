"""The scalp models.

Each is an independent hypothesis about a short-lived inefficiency, and each answers
only "is my pattern here, and where do the stop and target go". Scoring, costing,
gating, sizing and correlation all happen once, downstream, for every model alike.

They share detection engines with the A/A+ engine — the same FVG, order block,
liquidity and structure code, read on M1/M5 through `MicroSnapshot`. None of them
reimplements a detector, so a fix to sweep detection fixes it everywhere at once.

**Every model ships disabled.** `hypothesised_regimes` is a starting point for the
out-of-sample sweep, not a validated claim, and `ScalpConfig.enabled_models` is empty
by default. A model earns its way into that list by clearing the gate in `docs/specs`,
not by looking reasonable here.
"""

from __future__ import annotations

from xauusd.core.micro_structure import STRUCTURE_TF, TRIGGER_TF, MicroSnapshot
from xauusd.domain.enums import Direction, Regime, StructureKind
from xauusd.domain.types import MarketSnapshot
from xauusd.strategy.scalp.base import (
    ScalpFactors,
    ScalpModelMeta,
    ScalpSignal,
    clamp01,
    structural_target,
)
from xauusd.strategy.scalp.htf import read_htf

TREND_REGIMES = frozenset(
    {Regime.STRONG_BULL, Regime.MODERATE_BULL, Regime.MODERATE_BEAR, Regime.STRONG_BEAR}
)
ALL_TRADABLE = TREND_REGIMES | {Regime.RANGE}

# A shift older than this is history, not the reason price is where it is.
SHIFT_WINDOW_S = 20 * 60


def _obstacles(
    snap: MarketSnapshot, micro: MicroSnapshot, htf_obstacles: tuple[float, ...] = ()
) -> list[float]:
    """Everything that could stop price before the target.

    M1/M5 pools and S/R come first because they are nearest, but a scalp that aims
    through an H4 level is not a 1.5R trade — it is a trade that stalls at the level and
    exits on the time stop. The higher-timeframe obstacles are what make the difference,
    and they can only ever pull a target *in*, never push it out.
    """
    return (
        [p.price for p in micro.pools] + [lvl.price for lvl in snap.sr_levels] + list(htf_obstacles)
    )


def _session_factor(snap: MarketSnapshot) -> float:
    """Liquidity by session, as a soft factor rather than a gate.

    The session whitelist was removed because the clock is a proxy for the spread and
    the spread is measured directly. But time of day still carries information about
    how much follow-through to expect, so it survives here — weighted, not vetoing.
    """
    from xauusd.domain.enums import Session

    return {
        Session.OVERLAP: 1.0,
        Session.LONDON: 0.9,
        Session.NEW_YORK: 0.85,
        Session.ASIA: 0.4,
        Session.OFF: 0.2,
    }.get(snap.session.session, 0.3)


def _volatility_factor(micro: MicroSnapshot, cfg_min: float, cfg_max: float) -> float:
    """Enough movement to reach a target, not so much that stops are noise.

    Scored on the ratio of M1 to M5 ATR: a high ratio means the fast timeframe is
    carrying most of the range, which is expansion; a very low one means M1 is dead and
    a small target will not be reached before the time stop.
    """
    if micro.atr_m5 <= 0:
        return 0.0
    ratio = micro.atr_m1 / micro.atr_m5
    # 0.35-0.60 is the healthy band for a 5:1 timeframe ratio.
    if ratio < 0.2 or ratio > 0.9:
        return 0.2
    return clamp01(1.0 - abs(ratio - 0.45) / 0.45)


def _news_factor(snap: MarketSnapshot) -> float:
    """Lower is riskier. A blackout is a hard gate elsewhere; this is the gradient."""
    return {"NONE": 1.0, "LOW": 0.9, "MEDIUM": 0.6, "HIGH": 0.25, "EXTREME": 0.0}.get(
        str(snap.news.risk), 0.5
    )


def _dxy_factor(snap: MarketSnapshot, direction: Direction) -> float:
    """Gold against the dollar. Unknown is neutral-low, never neutral-high."""
    bias = getattr(snap.macro, "dxy_bias", None)
    if bias is None:
        return 0.4  # degradation is one-directional: unknown never helps
    sign = getattr(bias, "sign", 0)
    if sign == 0:
        return 0.5
    # A rising dollar is a headwind for a long in gold.
    aligned = (sign < 0) if direction is Direction.LONG else (sign > 0)
    return 0.9 if aligned else 0.15


def _stop_within_bounds(distance: float, atr_m5: float, lo: float, hi: float) -> bool:
    """A scalp stop must be structural but still a scalp.

    Too tight and the cost gate rejects it anyway; too wide and it is an intraday swing
    wearing a scalp's holding time, which the time stop will close at a random point.
    """
    if atr_m5 <= 0:
        return False
    return lo * atr_m5 <= distance <= hi * atr_m5


class _Base:
    """Shared plumbing. Not a model."""

    meta: ScalpModelMeta

    def __init__(self, settings=None) -> None:  # type: ignore[no-untyped-def]
        from xauusd.config.settings import Settings

        self.settings = settings or Settings()
        self.cfg = self.settings.scalp

    def _finish(
        self,
        micro: MicroSnapshot,
        snap: MarketSnapshot,
        direction: Direction,
        entry: float,
        stop: float,
        factors_partial: dict[str, float],
        evidence: dict[str, object],
        liquidity_ref: float | None = None,
        zone: tuple[float, float] | None = None,
    ) -> ScalpSignal | None:
        """Common tail: bound the stop, place the target, assemble the signal."""
        distance = abs(entry - stop)
        if not _stop_within_bounds(
            distance, micro.atr_m5, self.cfg.min_stop_atr, self.cfg.max_stop_atr
        ):
            return None
        if (direction is Direction.LONG and stop >= entry) or (
            direction is Direction.SHORT and stop <= entry
        ):
            return None

        # The H4/H1/M15 read, taken once and used three ways: it scores the signal, it
        # bounds the target, and it goes in the journal so a later reader can see which
        # timeframes agreed and which did not.
        htf = read_htf(snap, direction, entry, micro.atr_m5)

        target, rationale = structural_target(
            entry,
            stop,
            direction,
            self.cfg.target_rr,
            _obstacles(snap, micro, htf.obstacles),
        )
        # Record the ATR the stop was scaled against. Every structural threshold in the
        # system is ATR-scaled, so a journal entry without it cannot be re-derived later.
        evidence = {
            **evidence,
            "target_rationale": rationale,
            "atr_m5": micro.atr_m5,
            "atr_m1": micro.atr_m1,
            "stop_atr": distance / micro.atr_m5 if micro.atr_m5 > 0 else 0.0,
            "htf": htf.as_dict(),
        }

        factors = ScalpFactors(
            volatility=_volatility_factor(micro, self.cfg.min_stop_atr, self.cfg.max_stop_atr),
            session=_session_factor(snap),
            htf_context=htf.factor,
            news=_news_factor(snap),
            dxy=_dxy_factor(snap, direction),
            **factors_partial,
        )
        signal = ScalpSignal(
            model=self.meta.name,
            version=self.meta.version,
            direction=direction,
            entry=entry,
            stop_loss=stop,
            target=target,
            ts=micro.ts,
            factors=factors,
            evidence=evidence,
            liquidity_ref=liquidity_ref,
            zone_top=zone[0] if zone else None,
            zone_bottom=zone[1] if zone else None,
        )
        # A target pulled in behind an obstacle can leave less reward than risk; the
        # economic gates will judge it, but a non-positive target is not a candidate.
        return signal if signal.gross_rr > 0 else None


class LiquiditySweepReversal(_Base):
    """MODEL A — a pool is swept, price rejects, micro structure confirms the turn.

    The scalp analogue of the A/A+ chain, with two differences: the structure shift is
    read on M1 rather than M15, and the stop rests behind the sweep extreme on M5 where
    the cost model says a stop can afford to live.
    """

    meta = ScalpModelMeta(
        name="scalp_sweep_reversal",
        version="0.1",
        description="liquidity sweep, rejection, micro MSS, entry toward the reclaim",
        hypothesised_regimes=ALL_TRADABLE,
    )

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]:
        if not micro.usable:
            return []
        out: list[ScalpSignal] = []
        cutoff = micro.ts.timestamp() - SHIFT_WINDOW_S

        for direction in (Direction.LONG, Direction.SHORT):
            sweeps = [
                s
                for s in micro.sweeps
                if s.direction is direction and s.ts.timestamp() >= cutoff and s.quality >= 0.35
            ]
            if not sweeps:
                continue
            sweep = max(sweeps, key=lambda s: s.quality)

            shift = micro.recent_shift(direction, SHIFT_WINDOW_S, TRIGGER_TF)
            if shift is None or shift.ts < sweep.ts:
                continue  # the turn must follow the sweep, not precede it

            entry = snap.quote.mid
            extreme = (
                sweep.pool.price - sweep.penetration
                if direction is Direction.LONG
                else sweep.pool.price + sweep.penetration
            )
            buffer = 0.25 * micro.atr_m5
            stop = extreme - buffer if direction is Direction.LONG else extreme + buffer

            signal = self._finish(
                micro,
                snap,
                direction,
                entry,
                stop,
                {
                    "liquidity": clamp01(sweep.quality),
                    "market_structure": clamp01(shift.displacement_atr / 1.5),
                    "momentum": clamp01(sweep.displacement_after_atr),
                    "entry_location": clamp01(
                        1.0 - abs(entry - sweep.pool.price) / (2 * micro.atr_m5)
                    ),
                },
                {
                    "sweep_price": sweep.pool.price,
                    "sweep_quality": sweep.quality,
                    "shift": str(shift.kind),
                    "invalidation": (
                        f"a close beyond {stop:.2f} says the sweep of "
                        f"{sweep.pool.price:.2f} was not the reversal"
                    ),
                },
                liquidity_ref=sweep.pool.price,
            )
            if signal:
                out.append(signal)
        return out


class FvgRetracement(_Base):
    """MODEL C — displacement leaves a gap, price returns into it, entry at the edge.

    Entry sits at the edge price reaches FIRST on the retrace — the conservative fill
    assumption, since a limit deeper in the gap only fills if price traverses the whole
    thing. The stop then rests beyond the opposite edge, so the risk is the gap height
    plus a buffer.

    Putting the entry and the stop on the same edge is the obvious mistake here: it
    produces a stop distance equal to the buffer alone, which is both meaningless as
    invalidation and far too tight to survive the cost gate.
    """

    meta = ScalpModelMeta(
        name="scalp_fvg_retracement",
        version="0.1",
        description="unmitigated M5 FVG, price retracing into it with M1 confirmation",
        hypothesised_regimes=TREND_REGIMES,
    )

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]:
        if not micro.usable:
            return []
        out: list[ScalpSignal] = []
        price = snap.quote.mid

        for direction in (Direction.LONG, Direction.SHORT):
            gaps = [
                f
                for f in micro.fvgs
                if f.direction is direction
                and f.is_tradable
                and f.displacement_atr >= self.settings.fvg.min_displacement_atr
            ]
            if not gaps:
                continue
            # Nearest gap price is currently inside or approaching.
            gap = min(gaps, key=lambda f: abs(f.midpoint - price))
            if abs(gap.midpoint - price) > 1.5 * micro.atr_m5:
                continue

            buffer = 0.30 * micro.atr_m5
            if direction is Direction.LONG:
                entry, stop = gap.top, gap.bottom - buffer  # retrace down into the gap
            else:
                entry, stop = gap.bottom, gap.top + buffer  # retrace up into the gap

            shift = micro.recent_shift(direction, SHIFT_WINDOW_S, TRIGGER_TF)
            signal = self._finish(
                micro,
                snap,
                direction,
                entry,
                stop,
                {
                    "liquidity": 0.4,
                    "market_structure": clamp01(0.5 + (0.5 if shift else 0.0)),
                    "momentum": clamp01(gap.displacement_atr / 2.0),
                    "entry_location": clamp01(1.0 - abs(price - entry) / (1.5 * micro.atr_m5)),
                },
                {
                    "fvg_top": gap.top,
                    "fvg_bottom": gap.bottom,
                    "fvg_displacement_atr": gap.displacement_atr,
                    "m1_confirmation": bool(shift),
                    "invalidation": f"a close through {stop:.2f} fills and voids the gap",
                },
                zone=(gap.top, gap.bottom),
            )
            if signal:
                out.append(signal)
        return out


class OrderBlockReaction(_Base):
    """MODEL D — price returns to the last opposing candle before a break of structure.

    Entry at the edge price meets first, stop beyond the far edge — the same geometry
    as the FVG model, and for the same reason.
    """

    meta = ScalpModelMeta(
        name="scalp_ob_reaction",
        version="0.1",
        description="untested M5 order block, price reacting from it",
        hypothesised_regimes=ALL_TRADABLE,
    )

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]:
        if not micro.usable:
            return []
        out: list[ScalpSignal] = []
        price = snap.quote.mid

        for direction in (Direction.LONG, Direction.SHORT):
            blocks = [b for b in micro.order_blocks if b.direction is direction and b.is_tradable]
            if not blocks:
                continue
            ob = min(blocks, key=lambda b: abs((b.top + b.bottom) / 2 - price))
            if abs((ob.top + ob.bottom) / 2 - price) > 1.5 * micro.atr_m5:
                continue

            buffer = 0.30 * micro.atr_m5
            if direction is Direction.LONG:
                entry, stop = ob.top, ob.bottom - buffer
            else:
                entry, stop = ob.bottom, ob.top + buffer

            signal = self._finish(
                micro,
                snap,
                direction,
                entry,
                stop,
                {
                    "liquidity": 0.45,
                    "market_structure": clamp01(getattr(ob, "displacement_atr", 0.6) / 1.5),
                    "momentum": 0.5,
                    "entry_location": clamp01(1.0 - abs(price - entry) / (1.5 * micro.atr_m5)),
                },
                {
                    "ob_top": ob.top,
                    "ob_bottom": ob.bottom,
                    "invalidation": f"a close through {stop:.2f} means the block did not hold",
                },
                zone=(ob.top, ob.bottom),
            )
            if signal:
                out.append(signal)
        return out


class BreakoutRetest(_Base):
    """MODEL F — an intraday level breaks with displacement, then holds on the retest.

    The retest is the whole model. Entering on the break itself is how a false breakout
    becomes a loss; waiting for price to come back and hold turns the same level into a
    stop location.
    """

    meta = ScalpModelMeta(
        name="scalp_breakout_retest",
        version="0.1",
        description="M5 BOS with displacement, price retesting the broken level",
        hypothesised_regimes=TREND_REGIMES,
    )

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]:
        if not micro.usable:
            return []
        out: list[ScalpSignal] = []
        price = snap.quote.mid
        cutoff = micro.ts.timestamp() - SHIFT_WINDOW_S

        for direction in (Direction.LONG, Direction.SHORT):
            breaks = [
                e
                for e in micro.events_m5
                if e.kind is StructureKind.BOS
                and e.direction is direction
                and e.ts.timestamp() >= cutoff
            ]
            if not breaks:
                continue
            bos = breaks[-1]

            # The retest: price back within a quarter-ATR of the broken level.
            distance = abs(price - bos.price)
            if distance > 0.5 * micro.atr_m5:
                continue

            entry = price
            buffer = 0.35 * micro.atr_m5
            stop = bos.price - buffer if direction is Direction.LONG else bos.price + buffer

            signal = self._finish(
                micro,
                snap,
                direction,
                entry,
                stop,
                {
                    "liquidity": 0.35,
                    "market_structure": clamp01(bos.displacement_atr / 1.5),
                    "momentum": clamp01(bos.body_ratio),
                    "entry_location": clamp01(1.0 - distance / (0.5 * micro.atr_m5)),
                },
                {
                    "broken_level": bos.price,
                    "break_displacement_atr": bos.displacement_atr,
                    "invalidation": (
                        f"a close back through {stop:.2f} makes the break a false one"
                    ),
                },
                liquidity_ref=bos.price,
            )
            if signal:
                out.append(signal)
        return out


class MomentumContinuation(_Base):
    """MODEL H — a strong trend, a shallow pullback, and a resumption trigger.

    Deliberately restricted to strong trends. In a range, "shallow pullback" describes
    every bar, and the model would fire constantly on noise.
    """

    meta = ScalpModelMeta(
        name="scalp_momentum_continuation",
        version="0.1",
        description="strong-trend pullback with an M1 shift resuming the move",
        hypothesised_regimes=frozenset({Regime.STRONG_BULL, Regime.STRONG_BEAR}),
    )

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]:
        if not micro.usable or snap.regime not in self.meta.hypothesised_regimes:
            return []
        direction = Direction.LONG if snap.regime is Regime.STRONG_BULL else Direction.SHORT

        shift = micro.recent_shift(direction, SHIFT_WINDOW_S, TRIGGER_TF)
        if shift is None:
            return []

        last_bos = micro.last_event(STRUCTURE_TF, StructureKind.BOS)
        if last_bos is None or last_bos.direction is not direction:
            return []

        price = snap.quote.mid
        entry = price
        buffer = 0.30 * micro.atr_m5
        # Stop behind the pullback's origin — the broken level that started the leg.
        stop = last_bos.price - buffer if direction is Direction.LONG else last_bos.price + buffer

        signal = self._finish(
            micro,
            snap,
            direction,
            entry,
            stop,
            {
                "liquidity": 0.3,
                "market_structure": clamp01(last_bos.displacement_atr / 1.5),
                "momentum": clamp01(shift.displacement_atr / 1.2),
                "entry_location": clamp01(1.0 - abs(price - last_bos.price) / (2.5 * micro.atr_m5)),
            },
            {
                "trend_bos": last_bos.price,
                "resumption": str(shift.kind),
                "invalidation": f"a close beyond {stop:.2f} ends the trend leg",
            },
            liquidity_ref=last_bos.price,
        )
        return [signal] if signal else []


def default_scalp_registry(settings=None):  # type: ignore[no-untyped-def]
    """Every model, registered. None enabled — that is `ScalpConfig.enabled_models`."""
    from xauusd.strategy.scalp.base import ScalpRegistry

    registry = ScalpRegistry()
    for cls in (
        LiquiditySweepReversal,
        FvgRetracement,
        OrderBlockReaction,
        BreakoutRetest,
        MomentumContinuation,
    ):
        registry.register(cls(settings))
    return registry
