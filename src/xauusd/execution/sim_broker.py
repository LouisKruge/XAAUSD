"""Simulated broker for backtesting and paper trading.

Implements the same Broker interface as the live MT5 path, so strategy, scoring, risk
and order-management code is byte-identical across backtest and live. Only the fill
mechanics differ, and those are modelled explicitly rather than assumed:

  * entry fills at bid/ask derived from the recorded per-bar spread, plus slippage
  * commission charged per lot per side from the account's real schedule
  * SL and TP are resolved intrabar, conservatively (loss first) unless M1 data proves
    the true sequence
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from xauusd.domain.enums import Direction, ExitReason, OrderStatus, Timeframe
from xauusd.domain.types import (
    AccountState,
    Bar,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    Quote,
    SymbolSpec,
)
from xauusd.execution.broker import BrokerHealth
from xauusd.execution.retcodes import DONE, INVALID_STOPS, INVALID_VOLUME, NO_MONEY


@dataclass(slots=True)
class SimPosition:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    magic: int
    comment: str
    initial_sl: float
    commission_paid: float = 0.0
    swap: float = 0.0
    mae: float = 0.0  # worst adverse price excursion, in price units
    mfe: float = 0.0
    bars_held: int = 0

    def unrealised(self, price: float, spec: SymbolSpec) -> float:
        delta = (price - self.entry_price) * self.direction.sign
        return delta * spec.value_per_price_unit(self.volume)


@dataclass(slots=True)
class SimFillModel:
    """Explicit, tunable cost model. Every backtest records which values it used."""

    commission_per_lot: float = 7.0  # per lot, per side
    slippage_points_mean: float = 8.0
    slippage_points_std: float = 6.0
    slippage_points_max: float = 60.0
    latency_bars: int = 0
    spread_multiplier: float = 1.0  # stress testing: 2.0 doubles modelled spread
    slippage_multiplier: float = 1.0
    seed: int | None = 42

    _rng: random.Random | None = field(default=None, init=False, repr=False)

    def rng(self) -> random.Random:
        if self._rng is None:
            self._rng = random.Random(self.seed)
        return self._rng

    def slippage_points(self) -> float:
        """Slippage always works against us. Sign is applied by the caller."""
        s = abs(self.rng().gauss(self.slippage_points_mean, self.slippage_points_std))
        return min(s, self.slippage_points_max) * self.slippage_multiplier

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "commission_per_lot": self.commission_per_lot,
            "slippage_points_mean": self.slippage_points_mean,
            "slippage_points_std": self.slippage_points_std,
            "slippage_points_max": self.slippage_points_max,
            "spread_multiplier": self.spread_multiplier,
            "slippage_multiplier": self.slippage_multiplier,
            "latency_bars": self.latency_bars,
            "seed": self.seed,
        }


class SimBroker:
    """In-process broker simulation driven by an externally advanced clock."""

    def __init__(
        self,
        spec: SymbolSpec,
        starting_balance: float = 10_000.0,
        currency: str = "USD",
        fill_model: SimFillModel | None = None,
        default_spread_points: float = 25.0,
    ) -> None:
        self._spec = spec
        self._balance = starting_balance
        self._starting_balance = starting_balance
        self._currency = currency
        self._fills = fill_model or SimFillModel()
        self._default_spread_points = default_spread_points

        self._now: datetime = datetime(1970, 1, 1, tzinfo=UTC)
        self._bar: Bar | None = None
        self._spread_points: float = default_spread_points
        self._positions: dict[int, SimPosition] = {}
        self._next_ticket = 1
        self._history: list[dict[str, object]] = []
        self.closed_trades: list[dict[str, object]] = []
        self._bars_by_tf: dict[Timeframe, list[Bar]] = {}
        self.reject_next: int | None = None  # test hook: force a retcode

    # -- clock / data feed -------------------------------------------------------------

    def set_time(self, now: datetime, bar: Bar, spread_points: float | None = None) -> None:
        self._now = now
        self._bar = bar
        self._spread_points = (
            spread_points
            if spread_points is not None
            else (bar.spread_points or self._default_spread_points)
        ) * self._fills.spread_multiplier

    def set_bars(self, tf: Timeframe, bars: list[Bar]) -> None:
        self._bars_by_tf[tf] = bars

    @property
    def now(self) -> datetime:
        return self._now

    # -- Broker interface --------------------------------------------------------------

    def account(self) -> AccountState:
        eq = self._balance + sum(
            p.unrealised(self._mid(), self._spec) for p in self._positions.values()
        )
        used_margin = sum(
            (p.volume * self._spec.contract_size * p.entry_price) / 100.0
            for p in self._positions.values()
        )
        return AccountState(
            login=0,
            currency=self._currency,
            balance=self._balance,
            equity=eq,
            margin=used_margin,
            free_margin=eq - used_margin,
            margin_level=(eq / used_margin * 100.0) if used_margin > 0 else 0.0,
            ts=self._now,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._spec

    def quote(self, symbol: str) -> Quote:
        mid = self._mid()
        half = (self._spread_points * self._spec.point) / 2.0
        return Quote(ts=self._now, bid=mid - half, ask=mid + half)

    def bars(
        self, symbol: str, tf: Timeframe, count: int, end: datetime | None = None
    ) -> list[Bar]:
        series = self._bars_by_tf.get(tf, [])
        cutoff = end or self._now
        visible = [b for b in series if b.ts + timedelta(seconds=tf.seconds) <= cutoff]
        return visible[-count:] if count else visible

    def positions(self, magic: int | None = None) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                ticket=p.ticket,
                symbol=p.symbol,
                direction=p.direction,
                volume=p.volume,
                entry_price=p.entry_price,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                opened_at=p.opened_at,
                magic=p.magic,
                comment=p.comment,
                profit=p.unrealised(self._mid(), self._spec),
                commission=p.commission_paid,
                swap=p.swap,
            )
            for p in self._positions.values()
            if magic is None or p.magic == magic
        ]

    def send_market(self, req: OrderRequest) -> OrderResult:
        if self.reject_next is not None:
            code, self.reject_next = self.reject_next, None
            return OrderResult(False, OrderStatus.REJECTED, retcode=code, retcode_text="forced")

        spec = self._spec
        vol = spec.normalize_volume(req.volume)
        if vol < spec.volume_min or vol > spec.volume_max:
            return OrderResult(
                False,
                OrderStatus.REJECTED,
                INVALID_VOLUME,
                f"volume {req.volume} outside [{spec.volume_min}, {spec.volume_max}]",
            )

        q = self.quote(req.symbol)
        raw = q.price_for(req.direction)
        # Slippage always moves the fill against us.
        slip = self._fills.slippage_points() * spec.point * req.direction.sign
        fill = spec.normalize_price(raw + slip)

        if abs(fill - raw) / spec.point > req.max_slippage_points:
            return OrderResult(
                False, OrderStatus.REJECTED, 10004, "slippage exceeded max_slippage_points"
            )

        min_dist = spec.stops_level_price
        if req.stop_loss and abs(fill - req.stop_loss) < min_dist - 1e-9:
            return OrderResult(
                False,
                OrderStatus.REJECTED,
                INVALID_STOPS,
                f"SL {abs(fill - req.stop_loss):.4f} closer than stops_level {min_dist:.4f}",
            )
        if req.take_profit and abs(fill - req.take_profit) < min_dist - 1e-9:
            return OrderResult(False, OrderStatus.REJECTED, INVALID_STOPS, "TP inside stops_level")

        commission = self._fills.commission_per_lot * vol
        margin = self.calc_margin(req.symbol, req.direction, vol, fill) or 0.0
        if margin > self.account().free_margin:
            return OrderResult(False, OrderStatus.REJECTED, NO_MONEY, "insufficient free margin")

        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = SimPosition(
            ticket=ticket,
            symbol=req.symbol,
            direction=req.direction,
            volume=vol,
            entry_price=fill,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit or 0.0,
            opened_at=self._now,
            magic=req.magic,
            comment=req.comment,
            initial_sl=req.stop_loss,
            commission_paid=commission,
        )
        self._balance -= commission
        return OrderResult(
            True,
            OrderStatus.FILLED,
            DONE,
            "done",
            ticket=ticket,
            position_ticket=ticket,
            filled_volume=vol,
            fill_price=fill,
            raw={"slippage_points": abs(fill - raw) / spec.point},
        )

    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, OrderStatus.REJECTED, 10036, "position not found")
        spec = self._spec
        price = self._mid()
        if sl is not None:
            if abs(price - sl) < spec.stops_level_price - 1e-9:
                return OrderResult(False, OrderStatus.REJECTED, INVALID_STOPS, "SL too close")
            p.stop_loss = spec.normalize_price(sl)
        if tp is not None:
            p.take_profit = spec.normalize_price(tp)
        return OrderResult(True, OrderStatus.FILLED, DONE, "modified", ticket=ticket)

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(False, OrderStatus.REJECTED, 10036, "position not found")
        price = self.quote(p.symbol).exit_price_for(p.direction)
        self._close(p, price, ExitReason.MANUAL, self._now, volume)
        return OrderResult(
            True, OrderStatus.FILLED, DONE, "closed", ticket=ticket, fill_price=price
        )

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        """Mirrors MT5's order_calc_profit, used by the risk gate as a cross-check."""
        delta = (close_price - open_price) * direction.sign
        return delta * self._spec.value_per_price_unit(volume)

    def calc_margin(
        self, symbol: str, direction: Direction, volume: float, price: float
    ) -> float | None:
        return (volume * self._spec.contract_size * price) / 100.0

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=True, trade_allowed=True, trade_expert=True, last_tick_age_seconds=0.0
        )

    # -- simulation stepping -----------------------------------------------------------

    def step_bar(self, bar: Bar, m1_bars: list[Bar] | None = None) -> list[dict[str, object]]:
        """Advance one bar and resolve any SL/TP hits inside it.

        Sequencing rule: when both SL and TP fall inside the same bar's range, resolve
        the LOSS first unless M1 data proves otherwise. A backtest that assumes the
        favourable ordering manufactures an edge that does not exist.
        """
        self._bar = bar
        self._spread_points = (
            bar.spread_points or self._default_spread_points
        ) * self._fills.spread_multiplier
        events: list[dict[str, object]] = []

        for p in list(self._positions.values()):
            p.bars_held += 1
            adverse = bar.low if p.direction is Direction.LONG else bar.high
            favour = bar.high if p.direction is Direction.LONG else bar.low
            p.mae = max(p.mae, (p.entry_price - adverse) * p.direction.sign)
            p.mfe = max(p.mfe, (favour - p.entry_price) * p.direction.sign)

            hit_sl = self._touches(p.stop_loss, bar) if p.stop_loss else False
            hit_tp = self._touches(p.take_profit, bar) if p.take_profit else False

            if hit_sl and hit_tp:
                first = self._resolve_order_with_m1(p, bar, m1_bars)
            elif hit_sl:
                first = "SL"
            elif hit_tp:
                first = "TP"
            else:
                continue

            if first == "SL":
                # Stops fill at the stop price plus adverse slippage (gaps get worse).
                slip = self._fills.slippage_points() * self._spec.point
                price = p.stop_loss - slip * p.direction.sign
                if p.direction is Direction.LONG:
                    price = min(price, bar.high)
                else:
                    price = max(price, bar.low)
                self._close(p, price, ExitReason.STOP_LOSS, self._bar_close_time(bar))
                events.append({"ticket": p.ticket, "reason": "STOP_LOSS", "price": price})
            else:
                # Limit-style TP fills at the level, no positive slippage assumed.
                self._close(p, p.take_profit, ExitReason.TAKE_PROFIT, self._bar_close_time(bar))
                events.append({"ticket": p.ticket, "reason": "TAKE_PROFIT", "price": p.take_profit})
        return events

    def close_all(self, reason: ExitReason = ExitReason.END_OF_TEST) -> None:
        for p in list(self._positions.values()):
            self._close(p, self.quote(p.symbol).exit_price_for(p.direction), reason, self._now)

    # -- internals ---------------------------------------------------------------------

    def _mid(self) -> float:
        return self._bar.close if self._bar else 0.0

    @staticmethod
    def _bar_close_time(bar: Bar) -> datetime:
        return bar.ts

    @staticmethod
    def _touches(level: float, bar: Bar) -> bool:
        return bool(level) and bar.low <= level <= bar.high

    def _resolve_order_with_m1(self, p: SimPosition, bar: Bar, m1_bars: list[Bar] | None) -> str:
        """Use M1 data to find which level was reached first; fall back to LOSS FIRST."""
        if m1_bars:
            for m in m1_bars:
                sl_hit = self._touches(p.stop_loss, m)
                tp_hit = self._touches(p.take_profit, m)
                if sl_hit and tp_hit:
                    return "SL"  # still ambiguous at M1: stay conservative
                if sl_hit:
                    return "SL"
                if tp_hit:
                    return "TP"
        return "SL"

    def _close(
        self,
        p: SimPosition,
        price: float,
        reason: ExitReason,
        ts: datetime,
        volume: float | None = None,
    ) -> None:
        vol = min(volume, p.volume) if volume else p.volume
        delta = (price - p.entry_price) * p.direction.sign
        gross = delta * self._spec.value_per_price_unit(vol)
        portion = vol / p.volume if p.volume else 1.0
        commission = p.commission_paid * portion + self._fills.commission_per_lot * vol
        self._balance += gross - self._fills.commission_per_lot * vol

        risk_distance = abs(p.entry_price - p.initial_sl)
        risk_money = risk_distance * self._spec.value_per_price_unit(vol)

        self.closed_trades.append(
            {
                "ticket": p.ticket,
                "opened_at": p.opened_at,
                "closed_at": ts,
                "direction": p.direction,
                "entry": p.entry_price,
                "initial_sl": p.initial_sl,
                "exit_price": price,
                "volume": vol,
                "gross_pnl": gross,
                "commission": commission,
                "swap": p.swap,
                "risk_money": risk_money,
                "exit_reason": reason,
                "mae_r": (p.mae / risk_distance) if risk_distance > 0 else 0.0,
                "mfe_r": (p.mfe / risk_distance) if risk_distance > 0 else 0.0,
                "bars_held": p.bars_held,
                "comment": p.comment,
            }
        )
        if vol >= p.volume - 1e-9:
            self._positions.pop(p.ticket, None)
        else:
            p.volume -= vol
