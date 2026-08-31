"""Broker contract tests against a scripted bridge — no terminal, any OS.

The point of these is the failure paths. A trading system's behaviour after a failed
send matters more than its behaviour after a successful one.
"""

from __future__ import annotations

import time
from datetime import UTC

import pytest
from tests.fixtures.fake_bridge import FakeBridge, default_account, default_spec

from xauusd.domain.enums import Direction, OrderStatus, Timeframe
from xauusd.domain.types import OrderRequest
from xauusd.execution import retcodes as rc
from xauusd.execution.broker import AmbiguousSendError, BrokerError
from xauusd.execution.mt5_broker import Mt5Broker


@pytest.fixture
def bridge():
    b = FakeBridge()
    b.register("account", lambda **_: default_account())
    b.register("symbol_spec", lambda symbol, **_: default_spec(symbol))
    b.register("quote", lambda symbol, **_: {"ts": time.time(), "bid": 1999.75, "ask": 2000.25})
    b.start()
    yield b
    b.stop()


@pytest.fixture
def broker(bridge: FakeBridge) -> Mt5Broker:
    return Mt5Broker(f"127.0.0.1:{bridge.port}", magic=999)


class TestReads:
    def test_account(self, broker: Mt5Broker) -> None:
        a = broker.account()
        assert a.login == 12345678 and a.equity == 10_000.0 and a.trade_allowed

    def test_symbol_spec_is_read_not_assumed(self, broker: Mt5Broker) -> None:
        s = broker.symbol_spec("XAUUSD")
        assert s.contract_size == 100.0
        assert s.tick_value_loss == 1.0
        assert s.volume_step == 0.01
        assert s.stops_level == 10

    def test_spec_is_cached_but_refreshable(self, broker: Mt5Broker, bridge: FakeBridge) -> None:
        broker.symbol_spec("XAUUSD")
        broker.symbol_spec("XAUUSD")
        assert sum(1 for m, _ in bridge.calls if m == "symbol_spec") == 1
        broker.symbol_spec("XAUUSD", refresh=True)
        assert sum(1 for m, _ in bridge.calls if m == "symbol_spec") == 2

    def test_quote(self, broker: Mt5Broker) -> None:
        q = broker.quote("XAUUSD")
        assert q.ask > q.bid
        assert q.price_for(Direction.LONG) == 2000.25

    def test_bars_are_typed_and_utc(self, broker: Mt5Broker, bridge: FakeBridge) -> None:
        now = int(time.time())
        bridge.register(
            "bars",
            lambda **_: [
                {
                    "ts": now - 300 * i,
                    "open": 2000,
                    "high": 2005,
                    "low": 1995,
                    "close": 2002,
                    "tick_volume": 100,
                    "real_volume": 0,
                    "spread": 22,
                }
                for i in range(3)
            ],
        )
        bars = broker.bars("XAUUSD", Timeframe.M5, 3)
        assert len(bars) == 3
        assert all(b.ts.tzinfo is UTC for b in bars)
        assert bars[0].spread_points == 22


class TestSendSuccess:
    def test_successful_fill(self, broker: Mt5Broker, bridge: FakeBridge) -> None:
        bridge.register(
            "send_market",
            lambda **_: {
                "retcode": rc.DONE,
                "comment": "done",
                "order": 555,
                "deal": 777,
                "volume": 0.1,
                "price": 2000.30,
            },
        )
        r = broker.send_market(_req())
        assert r.ok and r.status is OrderStatus.FILLED and r.ticket == 555
        assert r.fill_price == 2000.30


class TestSendFailures:
    """Each class of failure must produce the right ACTION, not just an error."""

    @pytest.mark.parametrize(
        "retcode,expected",
        [
            (rc.REQUOTE, rc.RetAction.RETRY_REPRICE),
            (rc.PRICE_CHANGED, rc.RetAction.RETRY_REPRICE),
            (rc.TIMEOUT, rc.RetAction.RETRY_TRANSIENT),
            (rc.INVALID_STOPS, rc.RetAction.FIX_AND_RETRY),
            (rc.INVALID_VOLUME, rc.RetAction.FIX_AND_RETRY),
            (rc.INVALID_FILL, rc.RetAction.FIX_AND_RETRY),
            (rc.NO_MONEY, rc.RetAction.ABORT_AND_ALERT),
            (rc.MARKET_CLOSED, rc.RetAction.ABORT_AND_ALERT),
            (rc.TRADE_DISABLED, rc.RetAction.ABORT_AND_ALERT),
            (rc.FROZEN, rc.RetAction.KILL_SWITCH),
            (rc.SERVER_DISABLES_AT, rc.RetAction.KILL_SWITCH),
        ],
    )
    def test_retcode_maps_to_action(
        self, broker: Mt5Broker, bridge: FakeBridge, retcode: int, expected: rc.RetAction
    ) -> None:
        bridge.register("send_market", lambda **_: {"retcode": retcode, "comment": "x"})
        r = broker.send_market(_req())
        assert not r.ok
        assert rc.classify(r.retcode)[0] is expected

    def test_unmapped_retcode_is_never_optimistically_retried(self) -> None:
        assert rc.classify(45678)[0] is rc.RetAction.UNKNOWN


class TestAmbiguousSend:
    """The most dangerous case in the whole system: a send of unknown outcome."""

    def test_connection_drop_mid_send_is_ambiguous_not_failed(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        bridge.drop_methods.add("send_market")
        r = broker.send_market(_req())
        assert not r.ok
        assert r.status is OrderStatus.RECONCILING
        assert r.is_ambiguous
        assert "reconcile" in r.comment.lower()

    def test_bridge_reported_ambiguity_propagates(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        bridge.error_methods["send_market"] = "bridge timeout after 30s calling send_market"
        r = broker.send_market(_req())
        assert r.status is OrderStatus.RECONCILING

    def test_ground_truth_is_recovered_from_the_broker_by_tag(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        """After an ambiguous send, the BROKER decides whether the order exists."""
        bridge.drop_methods.add("send_market")
        req = _req(tag="abc123")
        r = broker.send_market(req)
        assert r.is_ambiguous

        # The order did in fact reach the server.
        bridge.positions = [
            {
                "ticket": 900,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.3,
                "sl": 1990.0,
                "tp": 2020.0,
                "time": time.time(),
                "magic": 999,
                "comment": "swp:abc123",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        found = broker.find_by_tag("abc123")
        assert found is not None and found.ticket == 900
        assert found.client_tag == "abc123"

    def test_no_position_found_means_the_order_never_landed(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        assert broker.find_by_tag("never-sent") is None

    def test_read_calls_are_not_treated_as_ambiguous(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        """A dropped READ is retried; only a dropped WRITE is ambiguous."""
        bridge.error_methods["account"] = "boom"
        with pytest.raises(BrokerError) as e:
            broker.account()
        assert not isinstance(e.value, AmbiguousSendError)


class TestHealth:
    def test_healthy(self, broker: Mt5Broker) -> None:
        h = broker.health()
        assert h.is_ok and h.connected and h.trade_allowed

    def test_unreachable_bridge_is_reported_not_raised(self) -> None:
        b = Mt5Broker("127.0.0.1:1", magic=1)
        h = b.health()
        assert not h.is_ok and h.last_tick_age_seconds > 1e8

    def test_autotrading_off_is_visible(self, broker: Mt5Broker, bridge: FakeBridge) -> None:
        bridge.register(
            "health",
            lambda **_: {
                "connected": True,
                "trade_allowed": False,
                "trade_expert": True,
                "last_tick_age_seconds": 0.1,
                "detail": "AutoTrading disabled",
            },
        )
        h = broker.health()
        assert not h.is_ok and h.connected


class TestCrossCheck:
    def test_calc_profit_is_available_for_sizing_verification(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        bridge.register("calc_profit", lambda **_: -100.0)
        assert broker.calc_profit("XAUUSD", Direction.LONG, 0.1, 2000, 1990) == -100.0

    def test_calc_profit_failure_returns_none_not_a_wrong_number(
        self, broker: Mt5Broker, bridge: FakeBridge
    ) -> None:
        bridge.error_methods["calc_profit"] = "not supported"
        assert broker.calc_profit("XAUUSD", Direction.LONG, 0.1, 2000, 1990) is None


def _req(tag: str = "tag1") -> OrderRequest:
    return OrderRequest(
        symbol="XAUUSD",
        direction=Direction.LONG,
        volume=0.1,
        price=2000.0,
        stop_loss=1990.0,
        take_profit=2020.0,
        client_tag=tag,
        magic=999,
        comment=f"swp:{tag}",
    )
