"""What a trade costs, and whether its expected value survives paying it.

The scalp engine's whole premise is smaller targets taken more often. That premise
lives or dies on one number: transaction cost as a share of the risk taken. Measured
against this broker's spec — 100oz contract, $7/lot commission — a round trip costs
about $0.47 an ounce. On a 30-point stop that is 157% of 1R: the trade is beaten before
it opens, and no win rate recovers it. On a 300-point stop it is 16%, and the strategy
gets to be judged on its merits.

So the A/A+ engine's `min_rr >= 2.0` is replaced here by something stricter in the way
that matters and permissive in the way the objective needs: **expected value must be
positive after real costs.** A 1:2 setup whose costs eat it is refused; a 1:1.25 setup
that clears them is allowed. That is the entire architectural argument for small
targets, expressed as arithmetic rather than as a preference.

Two rules:

**Costs are charged asymmetrically and never optimistically.** Spread is crossed on
entry; a take-profit limit does not slip but a market stop-out does, so the losing side
carries more cost than the winning one; commission is a round turn. An estimate that
flatters any of those produces a system that backtests well and loses money, which is
worse than one that never ships.

**A missing input never improves the answer.** No spread reading means the configured
maximum is assumed, not the median. Degradation is one-directional here as everywhere
else.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.domain.types import SymbolSpec


@dataclass(frozen=True, slots=True)
class TradeCosts:
    """Round-trip cost of one trade, in price and in units of its own risk.

    Costs are asymmetric, and the asymmetry is not a rounding detail. A take-profit is
    a resting limit: it fills at the price or not at all, so it does not slip. A stop
    is executed at market when it triggers, so it does. Charging slippage symmetrically
    understates the loss and overstates the win — in the direction that flatters the
    strategy, which is the direction that must never be assumed.
    """

    spread: float  # price, crossed once on entry
    entry_slippage: float  # price, market entry
    exit_slippage: float  # price, charged on stop-outs only
    commission: float  # price-equivalent, round turn
    stop_distance: float

    @property
    def cost_on_win(self) -> float:
        """Entry costs only — the take-profit limit does not slip."""
        return self.spread + self.entry_slippage + self.commission

    @property
    def cost_on_loss(self) -> float:
        """Entry costs plus the market exit the stop forces."""
        return self.cost_on_win + self.exit_slippage

    @property
    def total(self) -> float:
        """The worst case, which is the one a stop-out actually pays."""
        return self.cost_on_loss

    @property
    def as_fraction_of_risk(self) -> float:
        """Cost divided by 1R. Above 1.0 the trade cannot win at any win rate."""
        return self.total / self.stop_distance if self.stop_distance > 0 else float("inf")

    def net_rr(self, gross_rr: float) -> float:
        """Reward-to-risk after costs.

        The win pays the gross target minus entry costs; the loss pays the stop plus
        entry costs plus the slipped exit. Both sides move against the trade, which is
        why a 1:1 gross target is nothing like a 1:1 net one.
        """
        net_win = self.stop_distance * gross_rr - self.cost_on_win
        net_loss = self.stop_distance + self.cost_on_loss
        if net_loss <= 0:
            return 0.0
        return net_win / net_loss

    def net_expectancy_r(self, gross_rr: float, win_probability: float) -> float:
        """Expected R per trade after costs. This is the number that must be positive."""
        rr = self.net_rr(gross_rr)
        return win_probability * rr - (1.0 - win_probability)

    def break_even_win_rate(self, gross_rr: float) -> float:
        """The win rate at which this configuration merely breaks even, after costs."""
        rr = self.net_rr(gross_rr)
        return 1.0 / (1.0 + rr) if rr > 0 else float("inf")


class CostModel:
    """Turns live execution conditions into the cost of a specific proposed trade."""

    def __init__(
        self,
        spec: SymbolSpec,
        commission_per_lot: float,
        slippage_points: float,
        max_spread_points: float,
    ) -> None:
        self.spec = spec
        self.commission_per_lot = commission_per_lot
        self.slippage_points = slippage_points
        self.max_spread_points = max_spread_points

    def costs(self, stop_distance: float, spread_points: float | None = None) -> TradeCosts:
        """Cost of a trade risking `stop_distance` of price at the current spread.

        `spread_points=None` means the spread could not be read. That assumes the
        configured maximum rather than a typical value: an unknown execution condition
        must make the system less willing to trade, never more.
        """
        pts = self.max_spread_points if spread_points is None else spread_points
        slip = self.slippage_points * self.spec.point

        # Commission is quoted per lot; expressed per unit of price it is independent of
        # position size, because size scales the stop risk and the commission together.
        contract = self.spec.contract_size or 1.0

        return TradeCosts(
            spread=pts * self.spec.point,
            entry_slippage=slip,
            exit_slippage=slip,
            commission=self.commission_per_lot / contract,
            stop_distance=stop_distance,
        )

    def minimum_stop_for(
        self, max_cost_fraction: float, spread_points: float | None = None
    ) -> float:
        """The narrowest stop at which costs stay within `max_cost_fraction` of 1R.

        Used to explain a rejection in terms an operator can act on — "this setup needs
        a 235-point stop here and has 90" — rather than only refusing it.
        """
        if max_cost_fraction <= 0:
            raise ValueError("max_cost_fraction must be positive")
        return self.costs(1.0, spread_points).total / max_cost_fraction
