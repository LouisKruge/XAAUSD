"""MT5 return-code taxonomy.

A trading system's behaviour after a failed send matters more than its behaviour after
a successful one. Every code is classified into an action, and anything unmapped is
treated as UNKNOWN — which aborts and trips the kill switch rather than guessing.
"""

from __future__ import annotations

from enum import Enum

# MT5 TRADE_RETCODE_* constants. Hardcoded so the taxonomy is testable without the
# MetaTrader5 package installed (it is Windows-only).
REQUOTE = 10004
REJECT = 10006
CANCEL = 10007
PLACED = 10008
DONE = 10009
DONE_PARTIAL = 10010
ERROR = 10011
TIMEOUT = 10012
INVALID = 10013
INVALID_VOLUME = 10014
INVALID_PRICE = 10015
INVALID_STOPS = 10016
TRADE_DISABLED = 10017
MARKET_CLOSED = 10018
NO_MONEY = 10019
PRICE_CHANGED = 10020
PRICE_OFF = 10021
INVALID_EXPIRATION = 10022
ORDER_CHANGED = 10023
TOO_MANY_REQUESTS = 10024
NO_CHANGES = 10025
SERVER_DISABLES_AT = 10026
CLIENT_DISABLES_AT = 10027
LOCKED = 10028
FROZEN = 10029
INVALID_FILL = 10030
CONNECTION = 10031
ONLY_REAL = 10032
LIMIT_ORDERS = 10033
LIMIT_VOLUME = 10034
INVALID_ORDER = 10035
POSITION_CLOSED = 10036
INVALID_CLOSE_VOLUME = 10038
CLOSE_ORDER_EXIST = 10039
LIMIT_POSITIONS = 10040


class RetAction(str, Enum):
    """What the order manager is permitted to do next."""

    SUCCESS = "SUCCESS"
    RETRY_REPRICE = "RETRY_REPRICE"  # re-quote, re-validate RR, retry
    RETRY_TRANSIENT = "RETRY_TRANSIENT"  # backoff; RECONCILE before each retry
    FIX_AND_RETRY = "FIX_AND_RETRY"  # re-read spec, re-normalise, one retry
    ABORT = "ABORT"  # abandon this trade, journal, no retry
    ABORT_AND_ALERT = "ABORT_AND_ALERT"  # abandon + human attention
    KILL_SWITCH = "KILL_SWITCH"  # stop everything
    UNKNOWN = "UNKNOWN"  # unmapped: treated as KILL_SWITCH


_TAXONOMY: dict[int, tuple[RetAction, str]] = {
    DONE: (RetAction.SUCCESS, "request completed"),
    DONE_PARTIAL: (RetAction.SUCCESS, "request partially completed"),
    PLACED: (RetAction.SUCCESS, "order placed"),
    REQUOTE: (RetAction.RETRY_REPRICE, "requote"),
    PRICE_CHANGED: (RetAction.RETRY_REPRICE, "price changed"),
    PRICE_OFF: (RetAction.RETRY_REPRICE, "no quotes to process the request"),
    TIMEOUT: (RetAction.RETRY_TRANSIENT, "request timed out"),
    CONNECTION: (RetAction.RETRY_TRANSIENT, "no connection to trade server"),
    TOO_MANY_REQUESTS: (RetAction.RETRY_TRANSIENT, "too many requests"),
    INVALID_STOPS: (RetAction.FIX_AND_RETRY, "invalid stops"),
    INVALID_VOLUME: (RetAction.FIX_AND_RETRY, "invalid volume"),
    INVALID_FILL: (RetAction.FIX_AND_RETRY, "unsupported filling mode"),
    INVALID_PRICE: (RetAction.FIX_AND_RETRY, "invalid price"),
    REJECT: (RetAction.ABORT, "request rejected"),
    CANCEL: (RetAction.ABORT, "request cancelled by trader"),
    INVALID: (RetAction.ABORT, "invalid request"),
    INVALID_EXPIRATION: (RetAction.ABORT, "invalid expiration"),
    ORDER_CHANGED: (RetAction.ABORT, "order state changed"),
    NO_CHANGES: (RetAction.ABORT, "no changes in request"),
    POSITION_CLOSED: (RetAction.ABORT, "position already closed"),
    INVALID_ORDER: (RetAction.ABORT, "order state changed"),
    INVALID_CLOSE_VOLUME: (RetAction.ABORT, "close volume exceeds position volume"),
    CLOSE_ORDER_EXIST: (RetAction.ABORT, "a close order already exists"),
    NO_MONEY: (RetAction.ABORT_AND_ALERT, "insufficient funds"),
    MARKET_CLOSED: (RetAction.ABORT_AND_ALERT, "market is closed"),
    TRADE_DISABLED: (RetAction.ABORT_AND_ALERT, "trade is disabled"),
    LIMIT_ORDERS: (RetAction.ABORT_AND_ALERT, "orders limit reached"),
    LIMIT_VOLUME: (RetAction.ABORT_AND_ALERT, "volume limit reached"),
    LIMIT_POSITIONS: (RetAction.ABORT_AND_ALERT, "positions limit reached"),
    ONLY_REAL: (RetAction.ABORT_AND_ALERT, "operation allowed on live accounts only"),
    SERVER_DISABLES_AT: (RetAction.KILL_SWITCH, "autotrading disabled by server"),
    CLIENT_DISABLES_AT: (RetAction.KILL_SWITCH, "autotrading disabled by client terminal"),
    LOCKED: (RetAction.KILL_SWITCH, "request locked for processing"),
    FROZEN: (RetAction.KILL_SWITCH, "order or position frozen"),
    ERROR: (RetAction.KILL_SWITCH, "common error processing request"),
}


def classify(retcode: int) -> tuple[RetAction, str]:
    """Map a return code to an action. Unmapped codes are never optimistically retried."""
    return _TAXONOMY.get(retcode, (RetAction.UNKNOWN, f"unmapped retcode {retcode}"))


def is_success(retcode: int) -> bool:
    return classify(retcode)[0] is RetAction.SUCCESS


def retcode_name(retcode: int) -> str:
    return classify(retcode)[1]


# Filling modes (bitmask on SymbolInfo.filling_mode)
FILLING_FOK = 1
FILLING_IOC = 2
FILLING_RETURN = 4

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2


def supported_filling_modes(mask: int) -> list[int]:
    """Order the broker's supported filling modes by preference.

    Hardcoding IOC is a common cause of "the bot never fills at broker X"; this reads
    the broker's own bitmask and returns everything it accepts, best first.
    """
    modes: list[int] = []
    if mask & FILLING_IOC:
        modes.append(ORDER_FILLING_IOC)
    if mask & FILLING_FOK:
        modes.append(ORDER_FILLING_FOK)
    if mask & FILLING_RETURN:
        modes.append(ORDER_FILLING_RETURN)
    return modes or [ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN]
