"""Engine-side MT5 broker: implements Broker by talking to the bridge.

Runs anywhere (Linux included). Every MT5 quirk is handled here or in the bridge, so
the rest of the engine never sees one.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any

from xauusd.domain.enums import Direction, OrderStatus, Timeframe
from xauusd.domain.types import (
    AccountState,
    Bar,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    Quote,
    SymbolSpec,
)
from xauusd.execution.bridge_protocol import Request, encode, read_frame
from xauusd.execution.broker import AmbiguousSendError, BrokerError, BrokerHealth
from xauusd.execution.retcodes import is_success
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

POSITION_TYPE_BUY = 0


class BridgeTransport:
    """Reconnecting request/response client. One socket, one lock, ordered calls."""

    def __init__(self, address: str, timeout: float = 30.0) -> None:
        host, _, port = address.rpartition(":")
        self._host = host or "127.0.0.1"
        self._port = int(port)
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def _connect(self) -> socket.socket:
        s = socket.create_connection((self._host, self._port), timeout=self._timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        return s

    def close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        """Send one request. Raises AmbiguousSendError when the outcome is unknown."""
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            payload = dict(params or {})
            if timeout:
                payload["_timeout"] = timeout
            req = Request(method=method, params=payload, id=rid)
            for attempt in (1, 2):
                try:
                    sock = self._sock or self._connect()
                    sock.settimeout(timeout or self._timeout)
                    sock.sendall(encode(req))
                    frame = read_frame(sock)
                except (OSError, ConnectionError) as exc:
                    self._sock = None
                    # A mutating call that failed mid-flight has an UNKNOWN outcome.
                    if method in {"send_market", "close_position", "modify_position"}:
                        raise AmbiguousSendError(
                            f"{method} failed in flight ({exc}); outcome unknown - reconcile"
                        ) from exc
                    if attempt == 2:
                        raise BrokerError(f"bridge unreachable: {exc}") from exc
                    time.sleep(0.2)
                    continue
                if not frame.get("ok"):
                    if frame.get("error_kind") == "ambiguous":
                        raise AmbiguousSendError(str(frame.get("error")))
                    raise BrokerError(str(frame.get("error")))
                return frame.get("result")
        raise BrokerError("unreachable")


class Mt5Broker:
    """Broker implementation over the bridge."""

    def __init__(
        self, address: str = "127.0.0.1:50551", magic: int = 0, timeout: float = 30.0
    ) -> None:
        self.transport = BridgeTransport(address, timeout)
        self.magic = magic
        self._spec_cache: dict[str, SymbolSpec] = {}

    # -- reads -------------------------------------------------------------------------

    def account(self) -> AccountState:
        d = self.transport.call("account")
        return AccountState(
            login=int(d["login"]),
            currency=d["currency"],
            balance=float(d["balance"]),
            equity=float(d["equity"]),
            margin=float(d["margin"]),
            free_margin=float(d["free_margin"]),
            margin_level=float(d["margin_level"] or 0),
            leverage=int(d["leverage"]),
            trade_allowed=bool(d["trade_allowed"]),
            trade_expert=bool(d["trade_expert"]),
            server=d.get("server", ""),
            ts=datetime.now(UTC),
        )

    def symbol_spec(self, symbol: str, refresh: bool = False) -> SymbolSpec:
        if not refresh and symbol in self._spec_cache:
            return self._spec_cache[symbol]
        d = self.transport.call("symbol_spec", {"symbol": symbol})
        spec = SymbolSpec(
            symbol=d["symbol"],
            digits=int(d["digits"]),
            point=float(d["point"]),
            contract_size=float(d["contract_size"]),
            tick_size=float(d["tick_size"]),
            tick_value=float(d["tick_value"]),
            tick_value_profit=float(d["tick_value_profit"] or d["tick_value"]),
            tick_value_loss=float(d["tick_value_loss"] or d["tick_value"]),
            volume_min=float(d["volume_min"]),
            volume_max=float(d["volume_max"]),
            volume_step=float(d["volume_step"]),
            stops_level=int(d["stops_level"]),
            freeze_level=int(d["freeze_level"]),
            filling_modes=int(d["filling_modes"]),
            trade_mode=int(d["trade_mode"]),
            currency_profit=d["currency_profit"],
            currency_margin=d["currency_margin"],
            swap_long=float(d.get("swap_long") or 0),
            swap_short=float(d.get("swap_short") or 0),
            spread_points=int(d.get("spread_points") or 0),
            spread_float=bool(d.get("spread_float", True)),
        )
        self._spec_cache[symbol] = spec
        return spec

    def quote(self, symbol: str) -> Quote:
        d = self.transport.call("quote", {"symbol": symbol})
        return Quote(
            ts=datetime.fromtimestamp(float(d["ts"]), UTC),
            bid=float(d["bid"]),
            ask=float(d["ask"]),
        )

    def bars(
        self, symbol: str, tf: Timeframe, count: int, end: datetime | None = None
    ) -> list[Bar]:
        params: dict[str, Any] = {"symbol": symbol, "timeframe": str(tf), "count": count}
        if end:
            params["end_ts"] = end.timestamp()
        rows = self.transport.call("bars", params, timeout=60.0) or []
        return [
            Bar(
                ts=datetime.fromtimestamp(float(r["ts"]), UTC),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
                real_volume=int(r["real_volume"]),
                spread_points=int(r["spread"]),
            )
            for r in rows
        ]

    def positions(self, magic: int | None = None) -> list[BrokerPosition]:
        rows = self.transport.call(
            "positions", {"magic": magic if magic is not None else self.magic}
        )
        return [
            BrokerPosition(
                ticket=int(r["ticket"]),
                symbol=r["symbol"],
                direction=Direction.LONG if r["type"] == POSITION_TYPE_BUY else Direction.SHORT,
                volume=float(r["volume"]),
                entry_price=float(r["price_open"]),
                stop_loss=float(r["sl"]),
                take_profit=float(r["tp"]),
                opened_at=datetime.fromtimestamp(float(r["time"]), UTC),
                magic=int(r["magic"]),
                comment=r.get("comment", ""),
                profit=float(r.get("profit") or 0),
                swap=float(r.get("swap") or 0),
            )
            for r in rows or []
        ]

    def find_by_tag(
        self, client_tag: str, lookback_seconds: float = 600.0
    ) -> BrokerPosition | None:
        """Recover ground truth for an ambiguous send, by our deterministic tag.

        This is what makes 'never resend' safe: the broker, not our memory, decides
        whether the order exists.
        """
        for p in self.positions(magic=None):
            if client_tag and client_tag in (p.comment or ""):
                return p
        now = time.time()
        deals = (
            self.transport.call(
                "history_deals", {"from_ts": now - lookback_seconds, "to_ts": now + 60}
            )
            or []
        )
        for d in deals:
            if client_tag and client_tag in (d.get("comment") or ""):
                return BrokerPosition(
                    ticket=int(d.get("position_id") or 0),
                    symbol=d["symbol"],
                    direction=Direction.LONG if d["type"] == 0 else Direction.SHORT,
                    volume=float(d["volume"]),
                    entry_price=float(d["price"]),
                    stop_loss=0.0,
                    take_profit=0.0,
                    opened_at=datetime.fromtimestamp(float(d["time"]), UTC),
                    magic=int(d.get("magic") or 0),
                    comment=d.get("comment", ""),
                )
        return None

    # -- writes ------------------------------------------------------------------------

    def send_market(self, req: OrderRequest) -> OrderResult:
        try:
            d = self.transport.call(
                "send_market",
                {
                    "symbol": req.symbol,
                    "side": str(req.direction),
                    "volume": req.volume,
                    "price": req.price,
                    "sl": req.stop_loss,
                    "tp": req.take_profit,
                    "deviation": req.max_slippage_points,
                    "magic": req.magic,
                    "comment": req.comment,
                    "filling": req.filling_mode,
                },
                timeout=30.0,
            )
        except AmbiguousSendError as exc:
            log.error("send_ambiguous", client_tag=req.client_tag, error=str(exc))
            return OrderResult(
                False,
                OrderStatus.RECONCILING,
                retcode_text=str(exc),
                comment="outcome unknown - reconcile, do not resend",
            )
        retcode = int(d.get("retcode", 0))
        ok = is_success(retcode)
        return OrderResult(
            ok=ok,
            status=OrderStatus.FILLED if ok else OrderStatus.REJECTED,
            retcode=retcode,
            retcode_text=str(d.get("comment", "")),
            ticket=int(d.get("order") or 0) or None,
            position_ticket=int(d.get("order") or 0) or None,
            filled_volume=float(d.get("volume") or 0),
            fill_price=float(d.get("price") or 0),
            raw=d,
        )

    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> OrderResult:
        try:
            d = self.transport.call(
                "modify_position", {"ticket": ticket, "sl": sl, "tp": tp}, timeout=20.0
            )
        except AmbiguousSendError as exc:
            return OrderResult(False, OrderStatus.RECONCILING, retcode_text=str(exc))
        retcode = int(d.get("retcode", 0))
        return OrderResult(
            is_success(retcode),
            OrderStatus.FILLED if is_success(retcode) else OrderStatus.REJECTED,
            retcode,
            str(d.get("comment", "")),
            ticket=ticket,
        )

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        try:
            d = self.transport.call(
                "close_position", {"ticket": ticket, "volume": volume}, timeout=30.0
            )
        except AmbiguousSendError as exc:
            return OrderResult(False, OrderStatus.RECONCILING, retcode_text=str(exc))
        retcode = int(d.get("retcode", 0))
        return OrderResult(
            is_success(retcode),
            OrderStatus.FILLED if is_success(retcode) else OrderStatus.REJECTED,
            retcode,
            str(d.get("comment", "")),
            ticket=ticket,
            filled_volume=float(d.get("volume") or 0),
            fill_price=float(d.get("price") or 0),
        )

    def calc_profit(
        self,
        symbol: str,
        direction: Direction,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        try:
            v = self.transport.call(
                "calc_profit",
                {
                    "symbol": symbol,
                    "side": str(direction),
                    "volume": volume,
                    "open_price": open_price,
                    "close_price": close_price,
                },
            )
            return float(v) if v is not None else None
        except BrokerError:
            return None

    def calc_margin(
        self, symbol: str, direction: Direction, volume: float, price: float
    ) -> float | None:
        try:
            v = self.transport.call(
                "calc_margin",
                {"symbol": symbol, "side": str(direction), "volume": volume, "price": price},
            )
            return float(v) if v is not None else None
        except BrokerError:
            return None

    def health(self) -> BrokerHealth:
        t0 = time.perf_counter()
        try:
            d = self.transport.call("health", timeout=5.0)
        except BrokerError as exc:
            return BrokerHealth(False, False, False, 1e9, detail=str(exc))
        latency = int((time.perf_counter() - t0) * 1000)
        return BrokerHealth(
            connected=bool(d["connected"]),
            trade_allowed=bool(d["trade_allowed"]),
            trade_expert=bool(d["trade_expert"]),
            last_tick_age_seconds=float(d["last_tick_age_seconds"]),
            latency_ms=latency,
            detail=str(d.get("detail", "")),
        )

    def raw_symbols(self, patterns: list[str], override: str | None = None) -> list[dict[str, Any]]:
        d = self.transport.call("resolve_symbol", {"patterns": patterns, "override": override})
        return list(d.get("symbols", []))
