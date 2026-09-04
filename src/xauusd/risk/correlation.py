"""Correlation budgets: making ten positions ten bets rather than one bet sized ten.

Ten open XAUUSD scalps are not ten independent trades. They are one bet on gold, held
ten times, with ten commissions. Monte Carlo over 200,000 clusters of ten positions at
0.15% each: with independent signals a losing cluster never exhausts the 2% daily
limit, at a realistic correlation of 0.7 it does so on **14% of days**, and when every
position comes from the same signal in the same direction, on **32%**.

The mean cluster return is positive in all three cases. The problem is not negative
edge, it is concentrated variance — and concentrated variance turns a daily loss limit
from a backstop into something the normal operating mode walks into several times a
week.

So this is not a refinement to add later. It is the component that makes concurrency
above one defensible at all, and `ScalpConfig` refuses `max_concurrent > 1` until it
exists. Four budgets, each answering a different way two trades can turn out to be the
same trade:

    same direction   too much of the book facing one way
    same zone        two stops close enough that one move takes both
    same pool        two trades premised on the same liquidity event
    same setup burst one model firing repeatedly on one condition

None of them replaces the global open-risk cap. They sit in front of it, because a
book inside the cap can still be one undiversified position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from xauusd.domain.enums import Direction
from xauusd.domain.types import GateResult


@dataclass(frozen=True, slots=True)
class OpenExposure:
    """One position already on the book, as the correlation budgets see it."""

    direction: Direction
    risk_pct: float
    stop_price: float
    opened_at: datetime
    model: str = ""
    liquidity_ref: float | None = None


@dataclass(slots=True)
class CorrelationLimits:
    """Every budget is a fraction of the global open-risk cap, not an absolute.

    Expressing them as fractions means raising or lowering the global cap moves them
    together, and a change to the cap cannot silently widen a correlation budget.
    """

    max_total_open_risk_pct: float = 0.02
    same_direction_share: float = 0.60  # at most 60% of the book facing one way
    same_zone_atr: float = 1.0  # stops within this many ATR are one position
    same_setup_window_s: float = 300.0  # one model, one condition, one entry
    max_same_model: int = 3

    @property
    def same_direction_cap(self) -> float:
        return self.max_total_open_risk_pct * self.same_direction_share


@dataclass(slots=True)
class CorrelationDecision:
    approved: bool
    checks: tuple[GateResult, ...] = ()
    reason: str = ""
    blocking: list[str] = field(default_factory=list)


def evaluate_correlation(
    *,
    direction: Direction,
    risk_pct: float,
    stop_price: float,
    now: datetime,
    open_positions: list[OpenExposure],
    limits: CorrelationLimits,
    atr: float,
    model: str = "",
    liquidity_ref: float | None = None,
) -> CorrelationDecision:
    """Judge one proposed trade against what is already open.

    Every budget is evaluated even after one has failed, so the journal can answer
    "what else would have blocked this?" rather than naming only the first problem —
    the same rule the mandatory gates follow.
    """
    checks: list[GateResult] = []

    # --- same direction ---------------------------------------------------------
    same_dir = sum(p.risk_pct for p in open_positions if p.direction is direction)
    projected = same_dir + risk_pct
    cap = limits.same_direction_cap
    checks.append(
        GateResult(
            "corr.same_direction",
            projected <= cap,
            f"{projected:.3%} facing {direction}",
            f"<= {cap:.3%}",
            detail=(
                "the book is already concentrated in this direction; another adds "
                "exposure without adding diversification"
                if projected > cap
                else ""
            ),
        )
    )

    # --- same zone --------------------------------------------------------------
    # Two stops within a fraction of an ATR are one position: the move that takes one
    # takes the other, so the second trade doubles the loss without doubling the edge.
    near = [
        p for p in open_positions if abs(p.stop_price - stop_price) <= limits.same_zone_atr * atr
    ]
    checks.append(
        GateResult(
            "corr.same_zone",
            not near,
            f"{len(near)} open stop(s) within {limits.same_zone_atr:.2f} ATR",
            "none",
            detail=(
                f"an open stop at {near[0].stop_price:.2f} is close enough to this one "
                f"({stop_price:.2f}) that a single move takes both"
                if near
                else ""
            ),
        )
    )

    # --- same liquidity pool ----------------------------------------------------
    same_pool = [
        p
        for p in open_positions
        if liquidity_ref is not None
        and p.liquidity_ref is not None
        and abs(p.liquidity_ref - liquidity_ref) <= 0.25 * atr
    ]
    checks.append(
        GateResult(
            "corr.same_pool",
            not same_pool,
            f"{len(same_pool)} open trade(s) on this pool",
            "none",
            detail=(
                "a position is already premised on this liquidity event; a second is "
                "the same trade, not a second opinion"
                if same_pool
                else ""
            ),
        )
    )

    # --- same setup burst -------------------------------------------------------
    cutoff = now - timedelta(seconds=limits.same_setup_window_s)
    recent_same = [p for p in open_positions if p.model == model and p.opened_at >= cutoff]
    over_model = [p for p in open_positions if p.model == model]
    burst_ok = not recent_same and len(over_model) < limits.max_same_model
    checks.append(
        GateResult(
            "corr.same_setup",
            burst_ok,
            f"{len(over_model)} open from {model}, {len(recent_same)} in the window",
            f"< {limits.max_same_model} and none within {limits.same_setup_window_s:.0f}s",
            detail=(
                "one model firing repeatedly on one market condition is one signal, not several"
                if not burst_ok
                else ""
            ),
        )
    )

    failed = [c.name for c in checks if not c.passed]
    return CorrelationDecision(
        approved=not failed,
        checks=tuple(checks),
        reason="" if not failed else next(c.detail or c.name for c in checks if not c.passed),
        blocking=failed,
    )
