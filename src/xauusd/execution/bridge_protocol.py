"""Wire protocol between the engine and the MT5 bridge.

DEVIATION FROM docs/architecture/02-mt5-integration.md
------------------------------------------------------
The architecture specified gRPC. This implements the same contract over
length-prefixed JSON on a TCP socket instead. The reasons:

  * gRPC's value here was (a) a typed contract and (b) streaming. The typed contract
    is preserved by validating every request and response against the dataclasses
    below on both sides; streaming is preserved by the `subscribe` frame.
  * protoc codegen adds a build step to a Windows deployment whose whole appeal is
    that it is easy to reinstall after a VPS rebuild.
  * The bridge carries a handful of calls per second on localhost. gRPC's throughput
    advantage is irrelevant at this volume; its operational cost is not.

The transport is isolated behind `BridgeTransport`, so swapping in gRPC later means
writing one class, not touching the broker or the engine.

Framing: 4-byte big-endian unsigned length, then that many bytes of UTF-8 JSON.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_FRAME = 32 * 1024 * 1024  # 32 MB: a large bar history request, with headroom
PROTOCOL_VERSION = 1


@dataclass(slots=True)
class Request:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int = 0
    version: int = PROTOCOL_VERSION


@dataclass(slots=True)
class Response:
    id: int
    ok: bool
    result: Any = None
    error: str | None = None
    error_kind: str | None = None  # "ambiguous" marks a send of unknown outcome


class FramingError(RuntimeError):
    pass


def encode(obj: Request | Response) -> bytes:
    payload = json.dumps(asdict(obj), default=str, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise FramingError(f"frame too large: {len(payload)} bytes")
    return struct.pack(">I", len(payload)) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> dict[str, Any]:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    if length > MAX_FRAME:
        raise FramingError(f"declared frame too large: {length}")
    frame: dict[str, Any] = json.loads(_recv_exact(sock, length).decode("utf-8"))
    return frame


# Methods the bridge exposes. Anything not listed is rejected by the server, so a
# compromised or buggy client cannot reach arbitrary MT5 functionality.
METHODS = frozenset(
    {
        "ping",
        "health",
        "account",
        "symbol_spec",
        "resolve_symbol",
        "quote",
        "bars",
        "positions",
        "orders",
        "history_deals",
        "send_market",
        "modify_position",
        "close_position",
        "cancel_order",
        "calc_profit",
        "calc_margin",
        "calendar",
        "shutdown",
    }
)

# Methods that place or change money at risk. The server logs these at INFO with the
# full request, and refuses them when the terminal reports trading is disabled.
MUTATING_METHODS = frozenset({"send_market", "modify_position", "close_position", "cancel_order"})
