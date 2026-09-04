"""Higher-timeframe context for the scalp tier: H4, H1 and M15.

The scalp engine triggers on M1 and rests its stop on M5 structure. That is where the
*timing* comes from and it is not negotiable — a ninety-minute trade cannot wait for an
H4 candle to close. But timing is not location, and until this module existed the two
were conflated: the only higher-timeframe input a scalp signal had was a single soft
bias factor worth five points out of a hundred, computed from H1/H4/D1 with the daily
carrying the most weight of the three. On a trade that is closed inside ninety minutes,
that ordering is backwards.

This module reads the timeframes that actually bound a ninety-minute move and returns
three separate things, because they are three different questions:

    alignment    do M15/H1/H4 agree with the direction?  (a soft score factor)
    confluence   is the ENTRY sitting on an HTF level that supports it?  (score factor)
    obstacles    what is between the entry and the target?  (target placement)

`obstacles` is the one that changes trades rather than scores. A scalp aiming 1.5R
through an H4 resistance is not a 1.5R trade; it is a trade that stalls at the level and
closes on the time stop. Feeding HTF levels into `structural_target` pulls the target in
front of them, which lowers gross RR and therefore makes the economic gates *harder* to
pass. That direction is deliberate: the higher-timeframe read is allowed to shrink a
target, never to justify a wider one.

Missing data never helps. An absent H4 structure scores neutral-low, not neutral, and an
entry with no HTF feature near it scores zero confluence rather than a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xauusd.domain.enums import Direction, Timeframe
from xauusd.domain.types import MarketSnapshot
from xauusd.strategy.scalp.base import clamp01

# Weighted toward the timeframes that bound a ninety-minute hold. D1 survives with a
# small weight because a violent daily trend is still information, but it no longer
# outvotes M15 and H1 on a trade none of its bars will outlive.
ALIGNMENT_WEIGHTS: tuple[tuple[Timeframe, float], ...] = (
    (Timeframe.M15, 0.35),
    (Timeframe.H1, 0.30),
    (Timeframe.H4, 0.25),
    (Timeframe.D1, 0.10),
)

# Timeframes whose levels and zones can stop a scalp before its target. M5 and M1 come
# from MicroSnapshot; these are the ones only the HTF read supplies.
OBSTACLE_TFS = frozenset({Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1})

# How close, in M5 ATR, an HTF feature must be to the entry to count as confluence.
# Wider than this and the "level" is not where the entry is, it is merely nearby.
CONFLUENCE_ATR = 0.50


@dataclass(frozen=True, slots=True)
class HtfContext:
    """What H4/H1/M15 say about one candidate, at one instant."""

    alignment: float
    confluence: float
    obstacles: tuple[float, ...] = ()
    aligned_with: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    confluence_notes: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def factor(self) -> float:
        """The single 0..1 number the scorer weights.

        Alignment dominates because it is available on every signal; confluence is a
        bonus that only some entries earn, and an entry that earns none should still be
        able to score on a clean directional read.
        """
        return clamp01(0.65 * self.alignment + 0.35 * self.confluence)

    def as_dict(self) -> dict[str, object]:
        return {
            "alignment": round(self.alignment, 3),
            "confluence": round(self.confluence, 3),
            "aligned_with": list(self.aligned_with),
            "conflicts_with": list(self.conflicts_with),
            "confluence_notes": list(self.confluence_notes),
            "missing": list(self.missing),
            "obstacles": len(self.obstacles),
        }


def _alignment(
    snap: MarketSnapshot, direction: Direction
) -> tuple[float, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Weighted agreement across M15/H1/H4/D1.

    A timeframe the analyzer never built is not neutral — it is unknown, and unknown
    scores half of what a genuinely neutral bias would. Nothing here can lift the factor
    by going missing.
    """
    score = 0.0
    total = 0.0
    aligned: list[str] = []
    against: list[str] = []
    missing: list[str] = []

    for tf, weight in ALIGNMENT_WEIGHTS:
        total += weight
        st = snap.structures.get(tf)
        if st is None:
            missing.append(str(tf))
            score += weight * 0.25  # unknown is worth less than neutral
            continue
        bias = st.bias
        if bias.conflicts_with(direction):
            against.append(str(tf))
            continue
        if bias.sign == 0:
            score += weight * 0.5
            continue
        score += weight
        aligned.append(str(tf))

    return (
        clamp01(score / total) if total > 0 else 0.0,
        tuple(aligned),
        tuple(against),
        tuple(missing),
    )


def _confluence(
    snap: MarketSnapshot, direction: Direction, entry: float, atr_m5: float
) -> tuple[float, tuple[str, ...]]:
    """Is the entry standing on something an H4/H1/M15 trader would defend?

    Four independent sources, each worth a share. They are counted rather than averaged
    so that one strong feature is enough to register while four are worth more than one.
    """
    if atr_m5 <= 0:
        return 0.0, ()

    tolerance = CONFLUENCE_ATR * atr_m5
    notes: list[str] = []
    hits = 0.0

    long = direction is Direction.LONG

    # 1. An HTF fair value gap in the trade's direction, containing or adjacent to entry.
    for fvg in snap.fvgs:
        if fvg.timeframe not in OBSTACLE_TFS or not fvg.is_tradable:
            continue
        if fvg.direction is not direction:
            continue
        if fvg.contains(entry) or min(abs(entry - fvg.top), abs(entry - fvg.bottom)) <= tolerance:
            hits += 1.0
            notes.append(f"{fvg.timeframe} FVG")
            break

    # 2. An HTF order block in the trade's direction.
    for ob in snap.order_blocks:
        if ob.timeframe not in OBSTACLE_TFS or not ob.is_tradable:
            continue
        if ob.direction is not direction:
            continue
        if ob.contains(entry) or min(abs(entry - ob.top), abs(entry - ob.bottom)) <= tolerance:
            hits += 1.0
            notes.append(f"{ob.timeframe} OB")
            break

    # 3. An HTF level on the right side: support under a long, resistance over a short.
    for lvl in snap.sr_levels:
        if lvl.timeframe not in OBSTACLE_TFS:
            continue
        if lvl.distance(entry) > tolerance and not lvl.contains(entry):
            continue
        supportive = (lvl.price <= entry) if long else (lvl.price >= entry)
        if supportive:
            hits += 1.0
            notes.append(f"{lvl.timeframe} {lvl.kind}")
            break

    # 4. Discount for a long, premium for a short, in the HTF dealing range. Buying the
    #    top of a range is the single most reliable way to pay for someone else's exit.
    dr = snap.dealing_range
    if dr is not None and dr.size > 0:
        position = dr.position_of(entry)
        if (long and position <= 0.5) or (not long and position >= 0.5):
            hits += 1.0
            notes.append(f"{dr.timeframe} {dr.zone_label(entry)}")

    return clamp01(hits / 4.0), tuple(notes)


def _obstacles(snap: MarketSnapshot, direction: Direction, entry: float) -> tuple[float, ...]:
    """HTF prices ahead of the entry that a scalp target should not aim through.

    Only what is *ahead* is returned: a level behind the entry cannot stop the trade
    reaching its target, and including it would let `structural_target` pull a target
    backwards past the entry.
    """
    long = direction is Direction.LONG
    out: list[float] = []

    for lvl in snap.sr_levels:
        if lvl.timeframe not in OBSTACLE_TFS:
            continue
        # The near edge of the band is what price meets first.
        price = lvl.band_lower if long else lvl.band_upper
        if (long and price > entry) or (not long and price < entry):
            out.append(price)

    for pool in snap.liquidity:
        if pool.timeframe not in OBSTACLE_TFS or not pool.is_resting:
            continue
        if (long and pool.price > entry) or (not long and pool.price < entry):
            out.append(pool.price)

    # An opposing HTF zone is where the move is most likely to be met. The near edge is
    # the obstacle; the far edge is already past the point the target should stop at.
    for fvg in snap.fvgs:
        if fvg.timeframe not in OBSTACLE_TFS or not fvg.is_tradable:
            continue
        if fvg.direction is direction:
            continue
        price = fvg.bottom if long else fvg.top
        if (long and price > entry) or (not long and price < entry):
            out.append(price)

    for ob in snap.order_blocks:
        if ob.timeframe not in OBSTACLE_TFS or not ob.is_tradable:
            continue
        if ob.direction is direction:
            continue
        price = ob.bottom if long else ob.top
        if (long and price > entry) or (not long and price < entry):
            out.append(price)

    return tuple(sorted(set(out), reverse=not long))


def read_htf(
    snap: MarketSnapshot,
    direction: Direction,
    entry: float,
    atr_m5: float,
) -> HtfContext:
    """The full H4/H1/M15 read for one candidate. Pure; no state, no look-ahead.

    Everything comes from `MarketSnapshot`, which was built by the analyzer from the
    same `MarketView` as the micro snapshot. Nothing here can reach a bar the decision
    instant could not see.
    """
    alignment, aligned, against, missing = _alignment(snap, direction)
    confluence, notes = _confluence(snap, direction, entry, atr_m5)
    return HtfContext(
        alignment=alignment,
        confluence=confluence,
        obstacles=_obstacles(snap, direction, entry),
        aligned_with=aligned,
        conflicts_with=against,
        confluence_notes=notes,
        missing=missing,
    )
