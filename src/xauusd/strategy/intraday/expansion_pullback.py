"""INTRADAY ENGINE — Trend Expansion + Pullback (spec §17-§28).

A genuinely different strategy from the scalp engine, not the same idea with a wider
target. §48 is emphatic about this, and the difference is in the QUESTION each asks:

    scalp     "has short-term liquidity just been taken, and is price snapping back?"
    intraday  "has a larger directional regime established itself, has expansion
               occurred, and can I enter the pullback before continuation?"

The scalp reacts to a dislocation that has already happened on M1. This waits for a
regime, then for that regime to prove itself with an expansion, then for the market to
come back to a level worth entering. Different evidence, different timeframes, different
holding period, different risk.

The sequence is strict and ordered (§22-§24). Each step must have happened BEFORE the
next is looked for, because the whole hypothesis is that continuation follows a
pullback that followed an expansion. Finding the three in any other order is finding a
coincidence.

    H4 regime  ->  H1 agreement  ->  expansion  ->  pullback  ->  confirmation  ->  entry

§28 is the constraint that keeps this from degenerating into the scalp engine: ONE entry
per directional setup. Once a setup has been consumed the engine waits for a NEW
expansion, never a second entry on the same one. That is what stops it becoming
pyramiding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from xauusd.config.settings import Settings
from xauusd.domain.enums import Direction, StructureKind, Timeframe
from xauusd.domain.types import MarketSnapshot, StructureEvent
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

# The timeframes this engine reads, named so nothing silently drifts onto M1.
CONTEXT_TF = (Timeframe.H4, Timeframe.H1)
SETUP_TF = Timeframe.M15
TRIGGER_TF = Timeframe.M5
# Where §26's "major liquidity" is read from. D1/W1 extremes and session highs/lows are
# what the spec names; H4 and M15 pools are admitted only because `_liquidity_target`
# already refuses anything inside the R:R floor, which is what excludes the micro-pools
# that used to be picked as targets.
TARGET_TFS = frozenset({Timeframe.W1, Timeframe.D1, Timeframe.H4, Timeframe.M15})


@dataclass(frozen=True, slots=True)
class SetupState:
    """Where a directional setup has got to. The engine's memory between cycles.

    Held rather than re-derived because the sequence is ordered in TIME: an expansion
    that happened forty minutes ago and a pullback happening now are the setup. Deriving
    both from the current snapshot alone would accept them in either order, which is a
    different and much weaker claim.
    """

    direction: Direction
    expansion_at: datetime
    expansion_price: float
    # Set once an entry has been taken on this setup. §28: one entry per setup, so a
    # consumed setup is dead until a NEW expansion replaces it.
    consumed: bool = False

    def is_stale(self, now: datetime, max_age_minutes: int) -> bool:
        return (now - self.expansion_at) > timedelta(minutes=max_age_minutes)


@dataclass(frozen=True, slots=True)
class IntradayEvaluation:
    """One pass of the sequence, with the step it reached. Recorded whether or not it
    produced a trade — a setup that got to 'pullback' and stopped is worth seeing."""

    reached: str
    direction: Direction | None = None
    reasons: tuple[str, ...] = ()
    entry: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    # Which of the two rules produced the target. A journal that says "major liquidity"
    # about a number that came from a fixed R:R fallback is a journal telling a story
    # about structure that was never there — and fifteen of nineteen targets on
    # synthetic data came from the fallback, so this is the common case, not the edge.
    target_source: str = ""

    @property
    def is_entry(self) -> bool:
        return self.reached == "entry"

    def as_dict(self) -> dict[str, object]:
        return {
            "reached": self.reached,
            "direction": str(self.direction) if self.direction else None,
            "reasons": list(self.reasons),
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "target_source": self.target_source,
        }


class ExpansionPullbackEngine:
    """The intraday strategy. Detects setups; decides nothing about risk or execution."""

    name = "intraday_expansion_pullback"
    version = "1.0.0"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.setup: SetupState | None = None

    # -- the ordered sequence ------------------------------------------------------------

    def evaluate(self, snap: MarketSnapshot, now: datetime) -> IntradayEvaluation:
        cfg = self.settings.intraday

        # STEP 1-2 (§20, §21): H4 regime, then H1 must AGREE. Stated as a hard rule —
        # 4H bullish with 1H bearish means no trade, not a weaker trade.
        direction, why = self._directional_bias(snap)
        if direction is None:
            self._forget("context lost")
            return IntradayEvaluation("context", reasons=(why,))

        # STEP 3 (§22): expansion. Do not enter simply because the context is aligned.
        # The market must have demonstrated actual directional strength.
        if self.setup is None or self.setup.direction is not direction:
            event = self._expansion(snap, direction)
            if event is None:
                return IntradayEvaluation(
                    "context", direction=direction, reasons=("no expansion yet",)
                )
            self.setup = SetupState(direction, event.ts, event.price)
            log.debug("intraday_expansion", direction=str(direction), price=event.price)

        setup = self.setup
        if setup.is_stale(now, cfg.setup_max_age_minutes):
            self._forget("expansion too old")
            return IntradayEvaluation(
                "context", direction=direction, reasons=("expansion is stale",)
            )

        # §28: one entry per setup. A consumed setup waits for a NEW expansion rather
        # than offering a second entry, which is what stops this becoming pyramiding.
        if setup.consumed:
            return IntradayEvaluation(
                "expansion",
                direction=direction,
                reasons=("setup already traded; waiting for a new expansion",),
            )

        # STEP 4 (§23): pullback into a structurally relevant zone.
        zone = self._pullback_zone(snap, direction)
        if zone is None:
            return IntradayEvaluation(
                "expansion", direction=direction, reasons=("no pullback into a valid zone",)
            )

        # STEP 5 (§24): the pullback must not have destroyed the structure, and the
        # trigger timeframe must show the move resuming.
        confirmed, confirm_why = self._confirmation(snap, direction)
        if not confirmed:
            return IntradayEvaluation("pullback", direction=direction, reasons=(confirm_why,))

        entry, stop, target, target_source = self._levels(snap, direction, zone)
        if stop is None or target is None:
            return IntradayEvaluation(
                "pullback",
                direction=direction,
                reasons=("no structural stop or target available",),
            )
        return IntradayEvaluation(
            "entry",
            direction=direction,
            entry=entry,
            stop_loss=stop,
            target=target,
            target_source=target_source,
        )

    def consume(self) -> None:
        """Mark the current setup as traded. §28: it may not produce a second entry."""
        if self.setup is not None:
            self.setup = SetupState(
                self.setup.direction,
                self.setup.expansion_at,
                self.setup.expansion_price,
                consumed=True,
            )

    def _forget(self, why: str) -> None:
        if self.setup is not None:
            log.debug("intraday_setup_dropped", reason=why)
        self.setup = None

    # -- steps ---------------------------------------------------------------------------

    def _directional_bias(self, snap: MarketSnapshot) -> tuple[Direction | None, str]:
        """§21: H4 and H1 must agree. Anything else is explicitly NO TRADE."""
        h4, h1 = snap.bias(Timeframe.H4), snap.bias(Timeframe.H1)
        if h4.sign == 0 or h1.sign == 0:
            return None, f"H4 {h4} / H1 {h1}: no directional context"
        if h4.sign != h1.sign:
            return None, f"H4 {h4} conflicts with H1 {h1}"
        return (Direction.LONG if h4.sign > 0 else Direction.SHORT), "aligned"

    def _expansion(self, snap: MarketSnapshot, direction: Direction) -> StructureEvent | None:
        """§22: a break of meaningful structure in the trend's direction.

        Read on the SETUP timeframe, not M1. An M1 break is what the scalp engine trades;
        using it here would collapse the two engines into one.
        """
        st = snap.structures.get(SETUP_TF)
        if st is None:
            return None
        for event in (st.last_bos, st.last_mss):
            if event is None:
                continue
            if event.direction is direction and event.kind in (
                StructureKind.BOS,
                StructureKind.MSS,
            ):
                return event
        return None

    def _pullback_zone(
        self, snap: MarketSnapshot, direction: Direction
    ) -> tuple[float, float] | None:
        """§23: price has retraced into a zone that means something structurally.

        Deliberately NOT "any FVG or order block" — the spec says so explicitly. The zone
        must be on the setup timeframe, unmitigated, and in the trade's direction, and
        price must actually be in it now.
        """
        price = snap.quote.mid
        for fvg in snap.fvgs:
            if fvg.timeframe is not SETUP_TF or not fvg.is_tradable:
                continue
            if fvg.direction is direction and fvg.contains(price):
                return (fvg.top, fvg.bottom)
        for ob in snap.order_blocks:
            if ob.timeframe is not SETUP_TF or not ob.is_tradable:
                continue
            if ob.direction is direction and ob.contains(price):
                return (ob.top, ob.bottom)
        return None

    def _confirmation(self, snap: MarketSnapshot, direction: Direction) -> tuple[bool, str]:
        """§24: the pullback has not broken the structure, and the move is resuming."""
        setup_st = snap.structures.get(SETUP_TF)
        if setup_st is not None and setup_st.bias.conflicts_with(direction):
            return False, f"{SETUP_TF} structure turned against the trade"

        trigger = snap.structures.get(TRIGGER_TF)
        if trigger is None:
            return False, f"no {TRIGGER_TF} structure to confirm with"
        last = trigger.last_event
        if last is None or last.direction is not direction:
            return False, f"no {TRIGGER_TF} confirmation in the trade's direction"
        return True, "confirmed"

    def _levels(
        self, snap: MarketSnapshot, direction: Direction, zone: tuple[float, float]
    ) -> tuple[float, float | None, float | None, str]:
        """§25 stop behind the setup-timeframe structure, §26 target at major liquidity.

        The stop is deliberately NOT a tight M1 swing: §25 says it must give the position
        room to develop, and the lot size adjusts to keep the money risked fixed.
        """
        entry = snap.quote.mid
        top, bottom = max(zone), min(zone)
        buffer = snap.volatility.atr_m15 * self.settings.intraday.stop_buffer_atr

        stop = (bottom - buffer) if direction is Direction.LONG else (top + buffer)
        risk = abs(entry - stop)
        if risk <= 0:
            return entry, None, None, ""

        # §26: aim at major liquidity, never at a level chosen because it produces a
        # flattering R:R. Structure first — the RR is whatever the structure allows, and
        # the minimum-RR gate decides whether that is enough.
        target = self._liquidity_target(snap, direction, entry, risk)
        source = "resting liquidity ahead (§26)"
        if target is None:
            rr = self.settings.intraday.fallback_target_rr
            source = f"no structural level clears the floor; fallback {rr:.2f}R"
            target = entry + risk * rr if direction is Direction.LONG else entry - risk * rr
        return entry, stop, target, source

    def _liquidity_target(
        self, snap: MarketSnapshot, direction: Direction, entry: float, risk: float
    ) -> float | None:
        """The nearest resting liquidity ahead that is far enough to be a target at all.

        The original rule was "nearest resting pool ahead, full stop", and it was wrong
        in a way that only showed up as frequency. Measured over five weeks of M5
        instants, it aimed at an M15 micro-pool eleven times out of nineteen completed
        setups; the median R:R at a completed setup was 1.12 and ten of the nineteen were
        then refused by the 1.5 floor. The engine was doing all the work of finding a
        setup and then aiming it at a level twenty minutes away.

        Two things were wrong, and §26 names both of them itself.

        **"Major" has to mean major.** §26 says previous day/week extremes and session
        highs/lows. An M15 equal-high is not that. It is included here only because it
        can still be a real draw when it is far enough to matter, which the second rule
        already establishes.

        **A level inside the R:R floor is not a target.** The engine may not trade to it
        — `min_rr` refuses — so treating it as *the* target means the nearest untradeable
        level vetoes a trade that had a perfectly good level further out. Skipping it is
        not choosing a level for a flattering ratio: nothing is invented, no price is
        moved, and the ratio is still whatever the structure gives. It is declining to
        aim at something the engine is not allowed to aim at.

        Returns None when no level ahead qualifies, which means "no structural target",
        never a guessed one — the caller falls back to a stated RR and the journal shows
        which of the two produced the number.
        """
        cfg = self.settings.intraday
        long = direction is Direction.LONG
        floor = risk * cfg.min_rr if cfg.target_must_clear_rr_floor else 0.0
        candidates = [
            p.price
            for p in snap.liquidity
            if p.is_resting
            and p.timeframe in TARGET_TFS
            and ((p.price > entry) if long else (p.price < entry))
            and abs(p.price - entry) >= floor
        ]
        if not candidates:
            return None
        return min(candidates) if long else max(candidates)
