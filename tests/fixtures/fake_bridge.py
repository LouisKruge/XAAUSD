"""A scripted MT5 bridge for contract tests.

Speaks the real wire protocol on a real socket, so the transport, framing, timeout and
ambiguity paths are all exercised. This is what lets execution logic be tested on any
OS with no MetaTrader5 package and no terminal.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from collections.abc import Callable
from typing import Any

from xauusd.execution.bridge_protocol import Response, encode, read_frame


class FakeBridge:
    """Programmable bridge. Register per-method behaviours, including failures."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.server: socketserver.ThreadingTCPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        # Failure injection
        self.hang_methods: set[str] = set()  # never respond (client-side timeout)
        self.drop_methods: set[str] = set()  # close the socket mid-call
        self.error_methods: dict[str, str] = {}  # respond with an error

        self.positions: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []

        self.handlers.setdefault("ping", lambda **_: {"pong": True})
        self.handlers.setdefault(
            "health",
            lambda **_: {
                "connected": True,
                "trade_allowed": True,
                "trade_expert": True,
                "last_tick_age_seconds": 0.1,
                "detail": "",
            },
        )
        self.handlers.setdefault("positions", lambda **kw: self.positions)
        self.handlers.setdefault("history_deals", lambda **kw: self.deals)

    def register(self, method: str, fn: Callable[..., Any]) -> None:
        self.handlers[method] = fn

    def start(self) -> str:
        bridge = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                sock: socket.socket = self.request
                sock.settimeout(20)
                while True:
                    try:
                        frame = read_frame(sock)
                    except Exception:
                        return
                    method = frame.get("method", "")
                    params = dict(frame.get("params") or {})
                    params.pop("_timeout", None)
                    bridge.calls.append((method, params))

                    if method in bridge.drop_methods:
                        sock.close()
                        return
                    if method in bridge.hang_methods:
                        time.sleep(30)
                        continue
                    if method in bridge.error_methods:
                        sock.sendall(
                            encode(
                                Response(
                                    frame.get("id", 0),
                                    False,
                                    error=bridge.error_methods[method],
                                    error_kind="ambiguous" if method == "send_market" else None,
                                )
                            )
                        )
                        continue
                    fn = bridge.handlers.get(method)
                    if fn is None:
                        sock.sendall(
                            encode(
                                Response(
                                    frame.get("id", 0), False, error=f"no handler for {method}"
                                )
                            )
                        )
                        continue
                    try:
                        result = fn(**params)
                        sock.sendall(encode(Response(frame.get("id", 0), True, result)))
                    except Exception as exc:
                        sock.sendall(encode(Response(frame.get("id", 0), False, error=str(exc))))

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        return f"127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def __enter__(self) -> FakeBridge:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def default_spec(symbol: str = "XAUUSD") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "digits": 2,
        "point": 0.01,
        "contract_size": 100.0,
        "tick_size": 0.01,
        "tick_value": 1.0,
        "tick_value_profit": 1.0,
        "tick_value_loss": 1.0,
        "volume_min": 0.01,
        "volume_max": 50.0,
        "volume_step": 0.01,
        "stops_level": 10,
        "freeze_level": 5,
        "filling_modes": 2,
        "trade_mode": 4,
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -5.0,
        "swap_short": 2.0,
        "spread_points": 25,
        "spread_float": True,
    }


def default_account(equity: float = 10_000.0) -> dict[str, Any]:
    return {
        "login": 12345678,
        "currency": "USD",
        "balance": equity,
        "equity": equity,
        "margin": 0.0,
        "free_margin": equity,
        "margin_level": 0.0,
        "leverage": 100,
        "trade_allowed": True,
        "trade_expert": True,
        "server": "Demo-Server",
    }
