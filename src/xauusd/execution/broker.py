"""Broker abstraction.

The engine depends on this Protocol and never on MetaTrader5. Three implementations:

    Mt5Broker    - live/demo, via the bridge (Windows)
    SimBroker    - backtest, in-process
    PaperBroker  - live data, simulated fills

The backtester is not a parallel universe: it is SimBroker behind this same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from xauusd.domain.enums import Direction, Timeframe
from xauusd.domain.types import (
    AccountState,
    Bar,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    Quote,
    SymbolSpec,
)


@dataclass(frozen=True, slots=True)
class BrokerHealth:
    connected: bool
    trade_allowed: bool
    trade_expert: bool
    last_tick_age_seconds: float
    latency_ms: int = 0
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.connected and self.trade_allowed and self.trade_expert


class BrokerError(RuntimeError):
    """Raised for broker faults that are not an order return code."""


class AmbiguousSendError(BrokerError):
    """A send whose outcome is unknown.

    NEVER resend on this. The caller must reconcile against the broker's own position
    and order state using the deterministic client tag.
    """


@runtime_checkable
class Broker(Protocol):
    """The complete surface the engine is allowed to use.

    Note what is NOT here: there is no `add_to_position`, no `average_down`, no
    `widen_stop`. Prohibited behaviours are absent from the interface rather than
    merely discouraged, so they cannot be reached by accident.
    """

    def account(self) -> AccountState: ...

    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    def quote(self, symbol: str) -> Quote: ...

    def bars(
        self, symbol: str, tf: Timeframe, count: int, end: datetime | None = None
    ) -> list[Bar]: ...

    def positions(self, magic: int | None = None) -> list[BrokerPosition]: ...

    def send_market(self, req: OrderRequest) -> OrderResult: ...

    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> OrderResult: ...

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult: ...

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        """The broker's own P/L maths, used to cross-check our position sizing.

        Returning None means 'cannot verify', which the risk gate treats as a
        configurable condition, not a silent pass.
        """
        ...

    def calc_margin(
        self, symbol: str, direction: Direction, volume: float, price: float
    ) -> float | None: ...

    def health(self) -> BrokerHealth: ...
