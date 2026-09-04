"""Is this account large enough to trade this symbol at all?

`PositionSizer` already refuses, per trade, to round a sub-minimum position up: it
floors to the volume step and rejects when the result is below `volume_min`, rather
than silently risking several percent on one minimum lot. That is the correct
behaviour and it is not changed here.

But it is a *per-trade* answer, and it arrives silently. On an account too small for
the instrument, every setup is rejected for the same structural reason, and what the
operator sees is a bot that never trades — indistinguishable from a strategy that is
merely selective. That has already been the most expensive confusion in this project.

So this module answers the question once, up front, in the operator's own terms:
**given this broker's minimum lot and a structurally honest stop, what does one trade
actually risk as a fraction of this equity, and how much equity would the configured
risk budget require?** A pre-flight that says "this account is too small by a factor of
80" is worth more than eighty identical rejection-ledger entries.

Nothing here can approve a trade. It only explains, before the fact, what the sizer is
going to keep saying.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.domain.types import SymbolSpec


@dataclass(frozen=True, slots=True)
class ViabilityReport:
    """What one minimum-lot trade costs this account, and what it would take to trade."""

    equity: float
    currency: str
    risk_pct_target: float
    stop_distance: float
    loss_per_min_lot: float
    forced_risk_pct: float  # what one minimum lot actually risks, as a share of equity
    min_viable_equity: float  # equity at which the target risk becomes reachable
    margin_per_min_lot: float | None
    concurrent_supported: int  # positions the risk budget allows, at minimum lot
    viable: bool

    @property
    def shortfall_multiple(self) -> float:
        """How many times too small the account is. 1.0 means exactly viable."""
        return self.min_viable_equity / self.equity if self.equity > 0 else float("inf")

    def lines(self) -> list[str]:
        """Human-readable pre-flight block. Deliberately blunt about the failure case."""
        out = [
            f"equity           : {self.equity:,.2f} {self.currency}",
            f"risk budget      : {self.risk_pct_target:.2%} per trade",
            f"structural stop  : {self.stop_distance:.2f} price",
            f"minimum lot risk : {self.loss_per_min_lot:,.2f} {self.currency} "
            f"= {self.forced_risk_pct:.1%} of equity",
        ]
        if self.margin_per_min_lot is not None:
            out.append(
                f"margin (min lot) : {self.margin_per_min_lot:,.2f} {self.currency}"
                f" = {self.margin_per_min_lot / self.equity:.1%} of equity"
                if self.equity > 0
                else ""
            )
        if self.viable:
            out.append(
                f"VERDICT          : viable — {self.concurrent_supported} concurrent "
                f"minimum-lot positions fit the risk budget"
            )
            return out

        out += [
            "VERDICT          : ACCOUNT NOT EXECUTIONALLY VIABLE UNDER CURRENT "
            "BROKER CONDITIONS",
            f"                   one minimum lot ({self.forced_risk_pct:.1%}) exceeds the "
            f"{self.risk_pct_target:.2%} budget by {self.forced_risk_pct / self.risk_pct_target:.0f}x",
            f"                   minimum viable equity at this stop and risk: "
            f"{self.min_viable_equity:,.2f} {self.currency} "
            f"({self.shortfall_multiple:.0f}x current)",
            "                   every trade will be refused by the position sizer. That "
            "is the sizer working,",
            "                   not a strategy that is being too selective.",
        ]
        return out


def assess_account(
    spec: SymbolSpec,
    equity: float,
    risk_pct: float,
    stop_distance: float,
    fx_rate_to_account: float = 1.0,
    currency: str = "USD",
    price: float | None = None,
    leverage: float | None = None,
) -> ViabilityReport:
    """Measure the account against the broker's minimum lot at a realistic stop.

    `stop_distance` is a *structurally honest* stop — the one the strategy would actually
    place — not the smallest stop that would make the arithmetic work. Choosing the stop
    to fit the account is precisely the failure this exists to surface.
    """
    if stop_distance <= 0:
        raise ValueError("stop_distance must be positive")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")

    ticks = stop_distance / spec.tick_size
    loss_per_lot = ticks * spec.tick_value_loss * fx_rate_to_account
    loss_per_min_lot = loss_per_lot * spec.volume_min

    forced = loss_per_min_lot / equity if equity > 0 else float("inf")
    min_viable = loss_per_min_lot / risk_pct

    margin = None
    if price is not None and leverage:
        margin = (spec.volume_min * spec.contract_size * price / leverage) * fx_rate_to_account

    budget = equity * risk_pct
    concurrent = int(budget // loss_per_min_lot) if loss_per_min_lot > 0 else 0

    return ViabilityReport(
        equity=equity,
        currency=currency,
        risk_pct_target=risk_pct,
        stop_distance=stop_distance,
        loss_per_min_lot=loss_per_min_lot,
        forced_risk_pct=forced,
        min_viable_equity=min_viable,
        margin_per_min_lot=margin,
        concurrent_supported=concurrent,
        viable=loss_per_min_lot <= budget,
    )
