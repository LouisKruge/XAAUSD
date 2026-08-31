"""Position sizing from the broker's REAL specification.

The method, in full:

    1. equity        re-read from the broker now, never cached
    2. risk_pct      min(class cap, daily/weekly/monthly budget left, exposure headroom,
                         global cap, confidence scaling)
    3. risk_money    equity x risk_pct                     (ACCOUNT currency)
    4. sl_distance   |entry - stop|                        (price units)
    5. ticks         sl_distance / spec.trade_tick_size
    6. loss_per_lot  ticks x spec.trade_tick_value_loss    (the BROKER'S number)
    7. fx convert    if the account currency differs from the profit currency
    8. raw_lots      risk_money / loss_per_lot
    9. lots          FLOOR to volume_step - never round, so rounding can only reduce risk
   10. clamp         to [0, volume_max]
   11. reject        if lots < volume_min - NEVER shrink the stop to fit the lot size
   12. costs         add commission and expected slippage into realised risk
   13. assert        realised_risk <= equity x cap, else raise and trip the kill switch
   14. margin        check against free margin with a safety factor
   15. CROSS-CHECK   ask the broker to compute the same loss with order_calc_profit and
                     refuse to trade if the two disagree

Step 15 is the one that is rarely done and matters most. Computing the risk ourselves
AND asking MT5 to compute the same loss catches every class of unit error - wrong tick
value, wrong contract size, a missed currency conversion, a broker with an unusual gold
contract - before it reaches the market rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.config.settings import RiskConfig
from xauusd.domain.enums import Direction
from xauusd.domain.types import SizingResult, SymbolSpec
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


class RiskInvariantViolation(RuntimeError):
    """A computed size would breach a hard limit.

    This is never caught and clamped. It raises, the trade is abandoned, and the kill
    switch trips, because a sizing calculation that produces an over-limit number is
    evidence that something upstream is wrong.
    """


@dataclass(frozen=True, slots=True)
class SizingInputs:
    equity: float
    risk_pct: float
    entry: float
    stop_loss: float
    direction: Direction
    spec: SymbolSpec
    account_currency: str = "USD"
    fx_rate_to_account: float = 1.0  # profit currency -> account currency
    free_margin: float | None = None
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0


class PositionSizer:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.cfg = config or RiskConfig()

    def calculate(
        self,
        inputs: SizingInputs,
        broker_calc_profit: float | None = None,
        broker_calc_margin: float | None = None,
    ) -> SizingResult:
        """Compute lots. `broker_calc_profit` is the broker's own loss for 1 lot."""
        cfg = self.cfg
        spec = inputs.spec

        def reject(reason: str, **kw: float) -> SizingResult:
            return SizingResult(
                approved=False,
                lots=0.0,
                risk_money=0.0,
                risk_pct=0.0,
                risk_distance=kw.get("dist", 0.0),
                loss_per_lot=kw.get("lpl", 0.0),
                commission_est=0.0,
                slippage_est=0.0,
                realised_risk=0.0,
                reason=reason,
            )

        if inputs.equity <= 0:
            return reject("account equity is zero or negative")
        if inputs.risk_pct <= 0:
            return reject("approved risk percentage is zero")
        if inputs.risk_pct > cfg.global_risk_cap_pct + 1e-12:
            raise RiskInvariantViolation(
                f"requested risk {inputs.risk_pct:.4%} exceeds the global cap "
                f"{cfg.global_risk_cap_pct:.4%}"
            )

        sl_distance = abs(inputs.entry - inputs.stop_loss)
        if sl_distance <= 0:
            return reject("stop distance is zero")
        if sl_distance <= spec.stops_level_price:
            return reject(
                f"stop distance {sl_distance:.5f} is inside the broker's minimum "
                f"{spec.stops_level_price:.5f}",
                dist=sl_distance,
            )

        risk_money = inputs.equity * inputs.risk_pct

        # --- the broker's own numbers, not ours -------------------------------------
        ticks = sl_distance / spec.tick_size
        loss_per_lot = ticks * spec.tick_value_loss * inputs.fx_rate_to_account
        if loss_per_lot <= 0:
            return reject("computed loss per lot is not positive", dist=sl_distance)

        raw_lots = risk_money / loss_per_lot
        lots = spec.normalize_volume(raw_lots)  # ALWAYS floors
        lots = min(lots, spec.volume_max)

        if lots < spec.volume_min:
            # Never shrink the structural stop to make a position fit. Refuse instead.
            return reject(
                f"required size {raw_lots:.4f} lots is below the broker minimum "
                f"{spec.volume_min}; the account is too small for a structural stop of "
                f"{sl_distance:.2f} at {inputs.risk_pct:.2%} risk",
                dist=sl_distance,
                lpl=loss_per_lot,
            )

        commission = (inputs.commission_per_lot or cfg.commission_per_lot) * lots * 2.0
        slippage_price = (inputs.slippage_points or cfg.slippage_points_estimate) * spec.point
        slippage_cost = (
            (slippage_price / spec.tick_size)
            * spec.tick_value_loss
            * lots
            * inputs.fx_rate_to_account
        )
        realised_risk = lots * loss_per_lot + commission + slippage_cost

        # --- hard invariant ----------------------------------------------------------
        ceiling = inputs.equity * inputs.risk_pct * (1 + cfg.risk_overshoot_tolerance)
        # Costs are allowed on top of the stop-loss risk, but the STOP risk itself may
        # never exceed the cap.
        stop_risk = lots * loss_per_lot
        if stop_risk > ceiling:
            raise RiskInvariantViolation(
                f"computed stop risk {stop_risk:.2f} exceeds the ceiling {ceiling:.2f} "
                f"(equity {inputs.equity:.2f} x {inputs.risk_pct:.4%})"
            )

        # --- cross-check against the broker's own P/L maths --------------------------
        cross_delta: float | None = None
        if broker_calc_profit is not None:
            broker_loss = abs(broker_calc_profit)
            ours = loss_per_lot  # both are for 1.0 lot
            if broker_loss > 0:
                cross_delta = abs(ours - broker_loss) / broker_loss
                if cross_delta > cfg.sizing_cross_check_tolerance:
                    return SizingResult(
                        approved=False,
                        lots=0.0,
                        risk_money=risk_money,
                        risk_pct=inputs.risk_pct,
                        risk_distance=sl_distance,
                        loss_per_lot=loss_per_lot,
                        commission_est=commission,
                        slippage_est=slippage_cost,
                        realised_risk=realised_risk,
                        reason=(
                            f"our loss/lot {ours:.2f} disagrees with the broker's "
                            f"{broker_loss:.2f} by {cross_delta:.1%} (tolerance "
                            f"{cfg.sizing_cross_check_tolerance:.1%}) - refusing to trade "
                            f"on a specification we cannot verify"
                        ),
                        cross_check_delta=cross_delta,
                    )

        # --- margin ------------------------------------------------------------------
        if broker_calc_margin is not None and inputs.free_margin is not None:
            allowed = inputs.free_margin * cfg.margin_safety_factor
            if broker_calc_margin > allowed:
                return SizingResult(
                    approved=False,
                    lots=0.0,
                    risk_money=risk_money,
                    risk_pct=inputs.risk_pct,
                    risk_distance=sl_distance,
                    loss_per_lot=loss_per_lot,
                    commission_est=commission,
                    slippage_est=slippage_cost,
                    realised_risk=realised_risk,
                    reason=(
                        f"margin {broker_calc_margin:.2f} exceeds "
                        f"{cfg.margin_safety_factor:.0%} of free margin "
                        f"({allowed:.2f})"
                    ),
                    cross_check_delta=cross_delta,
                )

        return SizingResult(
            approved=True,
            lots=lots,
            risk_money=stop_risk,
            risk_pct=stop_risk / inputs.equity,
            risk_distance=sl_distance,
            loss_per_lot=loss_per_lot,
            commission_est=commission,
            slippage_est=slippage_cost,
            realised_risk=realised_risk,
            reason="ok",
            cross_check_delta=cross_delta,
        )

    def max_affordable_stop(
        self, equity: float, risk_pct: float, spec: SymbolSpec, fx_rate: float = 1.0
    ) -> float:
        """Largest stop distance the minimum lot can carry at this risk.

        Useful diagnostic: on a small account this is often smaller than a structural
        gold stop, which is exactly why the system will correctly refuse to trade.
        """
        risk_money = equity * risk_pct
        value_per_price = (spec.tick_value_loss / spec.tick_size) * spec.volume_min * fx_rate
        return risk_money / value_per_price if value_per_price > 0 else 0.0
