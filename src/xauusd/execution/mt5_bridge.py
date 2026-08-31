"""The MT5 bridge server. RUNS ON WINDOWS ONLY, alongside a running MT5 terminal.

This is the only process in the system that imports MetaTrader5. It exists because the
binding is:
  * Windows-only and terminal-dependent
  * process-global (mt5.initialize binds the process, not an object)
  * not thread-safe (concurrent calls corrupt results and can crash the terminal)

So: one process, ONE worker thread, an explicit request queue. All concurrency lives on
the engine side of the socket.

Run with:  python -m xauusd.execution.mt5_bridge --config config/demo.yaml
"""

from __future__ import annotations

import argparse
import queue
import socket
import socketserver
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xauusd.execution.bridge_protocol import (
    METHODS,
    MUTATING_METHODS,
    Response,
    encode,
    read_frame,
)
from xauusd.monitoring.logging import configure_logging, get_logger

log = get_logger(__name__)

TF_MAP_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
    "MN1": "TIMEFRAME_MN1",
}


@dataclass(slots=True)
class _Job:
    method: str
    params: dict[str, Any]
    done: threading.Event
    result: Any = None
    error: str | None = None
    error_kind: str | None = None


class Mt5Worker(threading.Thread):
    """The single thread permitted to touch the MetaTrader5 module.

    Every call from every client is serialised through `_queue`. This is not a
    performance choice; it is the only safe way to use the binding.
    """

    def __init__(
        self,
        terminal_path: str | None = None,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
    ) -> None:
        super().__init__(name="mt5-worker", daemon=True)
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._terminal_path = terminal_path
        self._login = login
        self._password = password
        self._server = server
        self._mt5: Any = None
        self._connected = False
        self._stop = threading.Event()
        self._filling_cache: dict[str, int] = {}

    # -- public API (thread-safe) ------------------------------------------------------

    def submit(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> _Job:
        job = _Job(method=method, params=params, done=threading.Event())
        self._queue.put(job)
        if not job.done.wait(timeout):
            job.error = f"bridge timeout after {timeout}s calling {method}"
            # A timed-out MUTATING call has an unknown outcome. Say so explicitly so the
            # caller reconciles instead of resending.
            job.error_kind = "ambiguous" if method in MUTATING_METHODS else "timeout"
        return job

    def stop(self) -> None:
        self._stop.set()

    # -- thread body -------------------------------------------------------------------

    def run(self) -> None:
        self._connect()
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                handler = getattr(self, f"_do_{job.method}", None)
                if handler is None:
                    job.error = f"unknown method {job.method}"
                else:
                    job.result = handler(**job.params)
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                if job.method in MUTATING_METHODS:
                    job.error_kind = "ambiguous"
                log.error("bridge_call_failed", method=job.method, error=job.error)
            finally:
                job.done.set()

    # -- connection --------------------------------------------------------------------

    def _connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            log.error(
                "metatrader5_not_available",
                hint="The bridge must run on Windows with the MetaTrader5 package installed.",
            )
            return False
        self._mt5 = mt5
        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login:
            kwargs.update(login=int(self._login), password=self._password, server=self._server)
        if not mt5.initialize(**kwargs):
            log.error("mt5_initialize_failed", error=str(mt5.last_error()))
            self._connected = False
            return False
        term = mt5.terminal_info()
        acct = mt5.account_info()
        if term is None or acct is None:
            log.error("mt5_no_terminal_or_account")
            self._connected = False
            return False
        self._connected = True
        log.info(
            "mt5_connected",
            login=acct.login,
            server=acct.server,
            trade_allowed=term.trade_allowed,
            trade_expert=acct.trade_expert,
            company=acct.company,
        )
        if not term.trade_allowed:
            log.warning("autotrading_disabled", hint="The AutoTrading button is off.")
        return True

    def _ensure(self) -> Any:
        if not self._connected and not self._connect():
            raise RuntimeError("MT5 not connected")
        return self._mt5

    # -- handlers ----------------------------------------------------------------------

    def _do_ping(self) -> dict[str, Any]:
        return {"pong": True, "ts": datetime.now(UTC).isoformat()}

    def _do_health(self) -> dict[str, Any]:
        mt5 = self._mt5
        if mt5 is None or not self._connected:
            return {
                "connected": False,
                "trade_allowed": False,
                "trade_expert": False,
                "last_tick_age_seconds": 1e9,
                "detail": "not connected",
            }
        term = mt5.terminal_info()
        acct = mt5.account_info()
        return {
            "connected": bool(term and term.connected),
            "trade_allowed": bool(term and term.trade_allowed and acct and acct.trade_allowed),
            "trade_expert": bool(acct and acct.trade_expert),
            "last_tick_age_seconds": 0.0,
            "detail": "",
        }

    def _do_account(self) -> dict[str, Any]:
        mt5 = self._ensure()
        a = mt5.account_info()
        if a is None:
            raise RuntimeError("account_info returned None")
        return {
            "login": a.login,
            "currency": a.currency,
            "balance": a.balance,
            "equity": a.equity,
            "margin": a.margin,
            "free_margin": a.margin_free,
            "margin_level": a.margin_level,
            "leverage": a.leverage,
            "trade_allowed": a.trade_allowed,
            "trade_expert": a.trade_expert,
            "server": a.server,
        }

    def _do_resolve_symbol(self, patterns: list[str], override: str | None = None) -> dict:
        """Enumerate every symbol so the engine can auto-discover the gold contract."""
        mt5 = self._ensure()
        symbols = mt5.symbols_get()
        out = []
        for s in symbols or []:
            out.append(
                {
                    "name": s.name,
                    "description": s.description,
                    "path": s.path,
                    "currency_profit": s.currency_profit,
                    "currency_base": s.currency_base,
                    "trade_mode": s.trade_mode,
                    "digits": s.digits,
                    "spread": s.spread,
                    "visible": s.visible,
                }
            )
        return {"symbols": out, "override": override, "patterns": patterns}

    def _do_symbol_spec(self, symbol: str) -> dict[str, Any]:
        mt5 = self._ensure()
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select failed for {symbol}")
        s = mt5.symbol_info(symbol)
        if s is None:
            raise RuntimeError(f"symbol_info returned None for {symbol}")
        return {
            "symbol": s.name,
            "digits": s.digits,
            "point": s.point,
            "contract_size": s.trade_contract_size,
            "tick_size": s.trade_tick_size,
            "tick_value": s.trade_tick_value,
            "tick_value_profit": s.trade_tick_value_profit,
            "tick_value_loss": s.trade_tick_value_loss,
            "volume_min": s.volume_min,
            "volume_max": s.volume_max,
            "volume_step": s.volume_step,
            "stops_level": s.trade_stops_level,
            "freeze_level": s.trade_freeze_level,
            "filling_modes": s.filling_mode,
            "trade_mode": s.trade_mode,
            "currency_profit": s.currency_profit,
            "currency_margin": s.currency_margin,
            "swap_long": s.swap_long,
            "swap_short": s.swap_short,
            "spread_points": s.spread,
            "spread_float": s.spread_float,
        }

    def _do_quote(self, symbol: str) -> dict[str, Any]:
        mt5 = self._ensure()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"no tick for {symbol}")
        return {"ts": t.time_msc / 1000.0, "bid": t.bid, "ask": t.ask, "last": t.last}

    def _do_bars(
        self, symbol: str, timeframe: str, count: int, end_ts: float | None = None
    ) -> list[dict[str, Any]]:
        mt5 = self._ensure()
        tf = getattr(mt5, TF_MAP_NAMES[timeframe])
        if end_ts:
            rates = mt5.copy_rates_from(symbol, tf, datetime.fromtimestamp(end_ts, UTC), count)
        else:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None:
            raise RuntimeError(f"copy_rates failed: {mt5.last_error()}")
        return [
            {
                "ts": float(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
                "real_volume": int(r["real_volume"]),
                "spread": int(r["spread"]),
            }
            for r in rates
        ]

    def _do_positions(self, magic: int | None = None) -> list[dict[str, Any]]:
        mt5 = self._ensure()
        positions = mt5.positions_get() or []
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "time": p.time,
                "magic": p.magic,
                "comment": p.comment,
                "profit": p.profit,
                "swap": p.swap,
            }
            for p in positions
            if magic is None or p.magic == magic
        ]

    def _do_orders(self, magic: int | None = None) -> list[dict[str, Any]]:
        mt5 = self._ensure()
        orders = mt5.orders_get() or []
        return [
            {
                "ticket": o.ticket,
                "symbol": o.symbol,
                "type": o.type,
                "volume": o.volume_current,
                "price_open": o.price_open,
                "sl": o.sl,
                "tp": o.tp,
                "magic": o.magic,
                "comment": o.comment,
            }
            for o in orders
            if magic is None or o.magic == magic
        ]

    def _do_history_deals(self, from_ts: float, to_ts: float) -> list[dict[str, Any]]:
        """Used to recover the true outcome of an ambiguous send."""
        mt5 = self._ensure()
        deals = (
            mt5.history_deals_get(
                datetime.fromtimestamp(from_ts, UTC),
                datetime.fromtimestamp(to_ts, UTC),
            )
            or []
        )
        return [
            {
                "ticket": d.ticket,
                "order": d.order,
                "position_id": d.position_id,
                "symbol": d.symbol,
                "type": d.type,
                "entry": d.entry,
                "volume": d.volume,
                "price": d.price,
                "commission": d.commission,
                "swap": d.swap,
                "profit": d.profit,
                "time": d.time,
                "magic": d.magic,
                "comment": d.comment,
            }
            for d in deals
        ]

    def _do_send_market(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        sl: float,
        tp: float | None,
        deviation: int,
        magic: int,
        comment: str,
        filling: int | None = None,
    ) -> dict[str, Any]:
        mt5 = self._ensure()
        term = mt5.terminal_info()
        if term is None or not term.trade_allowed:
            raise RuntimeError("terminal reports trading is not allowed (AutoTrading off)")

        order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
        info = mt5.symbol_info(symbol)
        candidates = [filling] if filling is not None else self._filling_candidates(symbol, info)

        last: dict[str, Any] = {}
        for fill_mode in candidates:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": order_type,
                "price": float(price),
                "sl": float(sl),
                "deviation": int(deviation),
                "magic": int(magic),
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fill_mode,
            }
            if tp:
                request["tp"] = float(tp)
            log.info("mt5_order_send", **{k: v for k, v in request.items() if k != "comment"})
            result = mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"order_send returned None: {mt5.last_error()}")
            last = {
                "retcode": result.retcode,
                "comment": result.comment,
                "order": result.order,
                "deal": result.deal,
                "volume": result.volume,
                "price": result.price,
                "filling_used": fill_mode,
            }
            # 10030 == TRADE_RETCODE_INVALID_FILL: try the next mode the broker declares.
            if result.retcode != 10030:
                if result.retcode in (10008, 10009, 10010):
                    self._filling_cache[symbol] = fill_mode
                return last
        return last

    def _filling_candidates(self, symbol: str, info: Any) -> list[int]:
        from xauusd.execution.retcodes import supported_filling_modes

        if symbol in self._filling_cache:
            return [self._filling_cache[symbol]]
        return supported_filling_modes(getattr(info, "filling_mode", 0))

    def _do_modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> dict[str, Any]:
        mt5 = self._ensure()
        pos = next((p for p in (mt5.positions_get(ticket=ticket) or [])), None)
        if pos is None:
            raise RuntimeError(f"position {ticket} not found")
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": pos.symbol,
            "sl": float(sl if sl is not None else pos.sl),
            "tp": float(tp if tp is not None else pos.tp),
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"modify returned None: {mt5.last_error()}")
        return {"retcode": result.retcode, "comment": result.comment}

    def _do_close_position(self, ticket: int, volume: float | None = None) -> dict[str, Any]:
        mt5 = self._ensure()
        pos = next((p for p in (mt5.positions_get(ticket=ticket) or [])), None)
        if pos is None:
            raise RuntimeError(f"position {ticket} not found")
        close_type = (
            mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        )
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(ticket),
            "symbol": pos.symbol,
            "volume": float(volume or pos.volume),
            "type": close_type,
            "price": float(price),
            "deviation": 30,
            "magic": pos.magic,
            "comment": "close",
            "type_filling": self._filling_cache.get(pos.symbol, mt5.ORDER_FILLING_IOC),
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"close returned None: {mt5.last_error()}")
        return {
            "retcode": result.retcode,
            "comment": result.comment,
            "price": result.price,
            "volume": result.volume,
        }

    def _do_cancel_order(self, ticket: int) -> dict[str, Any]:
        mt5 = self._ensure()
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        if result is None:
            raise RuntimeError("cancel returned None")
        return {"retcode": result.retcode, "comment": result.comment}

    def _do_calc_profit(
        self, symbol: str, side: str, volume: float, open_price: float, close_price: float
    ) -> float | None:
        """The broker's own P/L maths. The risk gate refuses to trade if ours disagrees."""
        mt5 = self._ensure()
        order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
        v = mt5.order_calc_profit(
            order_type, symbol, float(volume), float(open_price), float(close_price)
        )
        return float(v) if v is not None else None

    def _do_calc_margin(self, symbol: str, side: str, volume: float, price: float) -> float | None:
        mt5 = self._ensure()
        order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
        v = mt5.order_calc_margin(order_type, symbol, float(volume), float(price))
        return float(v) if v is not None else None

    def _do_calendar(self, from_ts: float, to_ts: float) -> list[dict[str, Any]]:
        """The terminal's built-in economic calendar, if this build exposes it.

        The Python package historically does not expose CalendarValueHistory; where it
        does not, the engine falls back to its provider chain (see 04-data-sources).
        """
        mt5 = self._ensure()
        fn = getattr(mt5, "calendar_value_history", None)
        if fn is None:
            return []
        values = (
            fn(
                datetime.fromtimestamp(from_ts, UTC),
                datetime.fromtimestamp(to_ts, UTC),
            )
            or []
        )
        out = []
        for v in values:
            ev = (
                mt5.calendar_event_by_id(v.event_id)
                if hasattr(mt5, "calendar_event_by_id")
                else None
            )
            out.append(
                {
                    "event_id": v.event_id,
                    "time": v.time,
                    "actual": v.actual_value,
                    "forecast": v.forecast_value,
                    "previous": v.prev_value,
                    "name": getattr(ev, "name", ""),
                    "importance": getattr(ev, "importance", 0),
                    "currency": getattr(ev, "currency", ""),
                }
            )
        return out

    def _do_shutdown(self) -> dict[str, bool]:
        self._stop.set()
        return {"stopping": True}


class _Handler(socketserver.BaseRequestHandler):
    worker: Mt5Worker

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(300)
        peer = self.client_address
        log.info("bridge_client_connected", peer=str(peer))
        try:
            while True:
                try:
                    frame = read_frame(sock)
                except (ConnectionError, OSError):
                    break
                rid = int(frame.get("id", 0))
                method = str(frame.get("method", ""))
                params = frame.get("params") or {}
                if method not in METHODS:
                    sock.sendall(
                        encode(Response(rid, False, error=f"method not allowed: {method}"))
                    )
                    continue
                job = self.worker.submit(
                    method, params, timeout=float(params.pop("_timeout", 30.0))
                )
                sock.sendall(
                    encode(
                        Response(
                            id=rid,
                            ok=job.error is None,
                            result=job.result,
                            error=job.error,
                            error_kind=job.error_kind,
                        )
                    )
                )
        finally:
            log.info("bridge_client_disconnected", peer=str(peer))


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 50551, **worker_kwargs: Any) -> None:
    worker = Mt5Worker(**worker_kwargs)
    worker.start()
    handler = type("BoundHandler", (_Handler,), {"worker": worker})
    server = ThreadedTCPServer((host, port), handler)
    log.info("bridge_listening", host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="MT5 bridge (Windows only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=50551)
    ap.add_argument("--terminal-path", default=None)
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument("--log-json", action="store_true")
    args = ap.parse_args()
    configure_logging("INFO", json_output=args.log_json, log_file="logs/bridge.log")
    serve(
        host=args.host,
        port=args.port,
        terminal_path=args.terminal_path,
        login=args.login,
        password=args.password,
        server=args.server,
    )


if __name__ == "__main__":
    main()
