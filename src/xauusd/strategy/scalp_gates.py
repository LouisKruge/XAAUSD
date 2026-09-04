"""Hard gates for the scalp engine.

The A/A+ engine refuses anything below 1:2. That floor is a good rule for a strategy
holding for hours and a bad one for a strategy holding for minutes, because it asks the
wrong question: it measures reward against risk and ignores what the trade costs to
open and close.

These gates ask the right one. A setup passes when its expected value is positive after
the spread, slippage and commission it will actually pay — which is *stricter* than
1:2 for a trade whose costs eat it, and permissive for a 1:1.25 trade that clears them.
That is what makes small targets safe rather than merely allowed.

They are hard gates, not scored factors. Everything soft belongs in the scalp score;
this file only contains conditions under which a trade is not worth making at any score.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.domain.types import GateResult
from xauusd.risk.cost_model import CostModel


@dataclass(slots=True)
class ScalpEconomics:
    """The economic facts about one proposed scalp, assembled before the gates run."""

    cost_model: CostModel
    stop_distance: float
    gross_rr: float
    win_probability: float
    spread_points: float | None = None
    max_cost_fraction: float = 0.35
    min_net_expectancy_r: float = 0.05

    def costs(self):  # type: ignore[no-untyped-def]
        return self.cost_model.costs(self.stop_distance, self.spread_points)


def g_cost_ratio(e: ScalpEconomics) -> GateResult:
    """Costs must not consume an unreasonable share of the risk taken.

    Separate from expectancy on purpose. A high enough assumed win rate can make almost
    any cost ratio look acceptable on paper, and the win rate is the least reliable
    input in the whole calculation. This gate does not consult it: it refuses trades
    whose economics depend on being right about probability, however good that estimate
    claims to be.
    """
    c = e.costs()
    frac = c.as_fraction_of_risk
    ok = frac <= e.max_cost_fraction
    detail = ""
    if not ok:
        needed = e.cost_model.minimum_stop_for(e.max_cost_fraction, e.spread_points)
        detail = (
            f"stop of {e.stop_distance:.2f} is too tight for these conditions; "
            f"{needed:.2f} would be needed, or wait for a narrower spread"
        )
    return GateResult(
        "scalp_cost_ratio",
        ok,
        f"{frac:.0%} of 1R",
        f"<= {e.max_cost_fraction:.0%}",
        detail=detail,
    )


def g_net_expectancy(e: ScalpEconomics) -> GateResult:
    """Expected value after costs must clear a positive floor.

    The floor is above zero deliberately. A setup with expectancy of +0.001R is
    indistinguishable from one with none, and paying a spread hundreds of times for a
    number that small is a way to convert an estimation error into a loss.
    """
    c = e.costs()
    ev = c.net_expectancy_r(e.gross_rr, e.win_probability)
    ok = ev >= e.min_net_expectancy_r
    return GateResult(
        "scalp_net_expectancy",
        ok,
        f"{ev:+.3f}R",
        f">= {e.min_net_expectancy_r:+.3f}R",
        detail=(
            f"net RR {c.net_rr(e.gross_rr):.2f} at p(win) {e.win_probability:.0%}; "
            f"break-even needs {c.break_even_win_rate(e.gross_rr):.1%}"
            if c.net_rr(e.gross_rr) > 0
            else "costs exceed the target — no win rate makes this trade positive"
        ),
    )


SCALP_ECONOMIC_GATES = [g_cost_ratio, g_net_expectancy]


def evaluate_economics(e: ScalpEconomics) -> list[GateResult]:
    """Run every economic gate, always. The trace must say what else would have failed."""
    return [gate(e) for gate in SCALP_ECONOMIC_GATES]
