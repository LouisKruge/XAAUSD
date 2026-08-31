"""Execution: the chaos suite.

Kill the bridge mid-send, drop the network, return a requote, desync the database —
in every case the system must end in a correct, reconciled state and must NEVER open a
duplicate position.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.fake_bridge import FakeBridge, default_account, default_spec
from xauusd.config.settings import ExecutionConfig, Settings
from xauusd.domain.enums import (
    Classification,
    Direction,
    KillSwitchReason,
    OrderStatus,
    Timeframe,
)
from xauusd.domain.types import (
    Decision,
    Quote,
    SizingResult,
    SymbolSpec,
    TargetLevel,
    TradePlan,
)
from xauusd.execution import retcodes as rc
from xauusd.execution.mt5_broker import Mt5Broker
from xauusd.execution.order_manager import OrderManager
from xauusd.execution.position_manager import (
    ManagedPosition,
    PositionManager,
    StopWideningRefused,
)
from xauusd.execution.reconciler import Reconciler
from xauusd.monitoring.alerts import Notifier
from xauusd.risk.kill_switch import KillSwitch

UTC = UTC
T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def gold() -> SymbolSpec:
    return SymbolSpec(
        "XAUUSD",
        2,
        0.01,
        100.0,
        0.01,
        1.0,
        1.0,
        1.0,
        0.01,
        50.0,
        0.01,
        10,
        5,
        commission_per_lot=7.0,
    )


def make_decision(entry: float = 2000.0, sl: float = 1990.0, tp: float = 2025.0) -> Decision:
    """Default TP is 2.5R, not exactly 2.0R.

    A plan sitting at exactly the 1:2 floor is ALWAYS rejected once the spread is
    applied, because the fill is on the far side of the quote. That is correct
    behaviour — see test_a_plan_at_exactly_the_floor_is_rejected — but it makes 2.0R
    a useless fixture for testing anything else.
    """
    p = TradePlan(
        "sweep_mss_fvg",
        "1.0",
        Direction.LONG,
        entry,
        sl,
        (TargetLevel(tp, abs(tp - entry) / abs(entry - sl), "PDH"),),
        T0,
        Timeframe.M15,
        "close below 1990",
        symbol="XAUUSD",
    )
    return Decision(
        ts=T0,
        symbol="XAUUSD",
        classification=Classification.A,
        mode="DEMO",
        plan=p,
        score=78.0,
        sizing=SizingResult(True, 0.10, 100.0, 0.01, 10.0, 1000.0, 1.4, 1.5, 102.9, "ok"),
    )


@pytest.fixture
def bridge():
    b = FakeBridge()
    b.register("account", lambda **_: default_account())
    b.register("symbol_spec", lambda symbol, **_: default_spec(symbol))
    b.register("quote", lambda symbol, **_: {"ts": time.time(), "bid": 1999.90, "ask": 2000.10})
    b.register(
        "send_market",
        lambda **_: {
            "retcode": rc.DONE,
            "comment": "done",
            "order": 555,
            "deal": 777,
            "volume": 0.1,
            "price": 2000.10,
        },
    )
    b.register("modify_position", lambda **_: {"retcode": rc.DONE, "comment": "ok"})
    b.register(
        "close_position",
        lambda **_: {"retcode": rc.DONE, "comment": "ok", "price": 2010.0, "volume": 0.1},
    )
    b.start()
    yield b
    b.stop()


@pytest.fixture
def manager(bridge):  # type: ignore[no-untyped-def]
    broker = Mt5Broker(f"127.0.0.1:{bridge.port}", magic=777)
    ks = KillSwitch()
    om = OrderManager(broker, Settings(), ks, Notifier())
    return om, broker, ks, bridge


class TestPreflight:
    def test_rejects_a_stale_quote(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, *_ = manager
        old = Quote(T0 - timedelta(seconds=60), 1999.9, 2000.1)
        ok, why, _ = om.preflight(make_decision().plan, 0.1, gold(), old, T0)
        assert not ok and "old" in why

    def test_rejects_a_wide_spread(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, *_ = manager
        wide = Quote(T0, 1999.0, 2001.0)  # 200 points
        ok, why, _ = om.preflight(make_decision().plan, 0.1, gold(), wide, T0)
        assert not ok and "spread" in why

    def test_abandons_rather_than_chases_a_drifting_price(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, *_ = manager
        drifted = Quote(T0, 2004.9, 2005.1)  # 0.5R away on a 10-point stop
        ok, why, _ = om.preflight(make_decision().plan, 0.1, gold(), drifted, T0)
        assert not ok and "chasing" in why

    def test_rr_is_rechecked_after_repricing_and_rounding(self, manager) -> None:  # type: ignore[no-untyped-def]
        """Slippage against us reduces RR; a trade that falls below 1:2 is abandoned."""
        om, *_ = manager
        d = make_decision(entry=2000.0, sl=1990.0, tp=2020.5)  # 2.05 RR at the signal
        moved = Quote(T0, 2000.9, 2001.1)  # fills 1.1 higher
        ok, why, repriced = om.preflight(d.plan, 0.1, gold(), moved, T0)
        assert not ok
        assert "reward-to-risk" in why and "abandoning" in why

    def test_accepts_a_clean_setup(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, *_ = manager
        ok, why, repriced = om.preflight(
            make_decision().plan, 0.1, gold(), Quote(T0, 1999.95, 2000.05), T0
        )
        assert ok, why
        assert repriced.rr >= 2.0

    def test_a_plan_at_exactly_the_floor_is_rejected_after_spread(self, manager) -> None:  # type: ignore[no-untyped-def]
        """A setup planned at exactly 1:2 cannot survive its own spread.

        The fill happens on the far side of the quote, so the realised RR is always
        slightly below the planned one. Strategies must therefore target ABOVE the
        floor, not at it — and the gate correctly refuses the difference.
        """
        om, *_ = manager
        exact = make_decision(tp=2020.0).plan  # exactly 2.0R at the signal price
        ok, why, _ = om.preflight(exact, 0.1, gold(), Quote(T0, 1999.95, 2000.05), T0)
        assert not ok
        assert "reward-to-risk" in why


class TestExecution:
    def test_successful_fill(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, *_ = manager
        out = om.execute(make_decision(), "tag001", gold(), T0)
        assert out.ok and out.status is OrderStatus.FILLED and out.ticket == 555

    def test_duplicate_is_refused_against_the_broker(self, manager) -> None:  # type: ignore[no-untyped-def]
        """The duplicate guard consults the BROKER, not our memory of what we sent."""
        om, broker, ks, bridge = manager
        bridge.positions = [
            {
                "ticket": 900,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.1,
                "sl": 1990.0,
                "tp": 2020.0,
                "time": time.time(),
                "magic": 777,
                "comment": "sweep_mss_fv:tag001",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        out = om.execute(make_decision(), "tag001", gold(), T0)
        assert not out.ok and "already exists" in out.reason

    @pytest.mark.parametrize("retcode", [rc.NO_MONEY, rc.MARKET_CLOSED, rc.TRADE_DISABLED])
    def test_terminal_rejections_abort_without_retry(self, manager, retcode) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        bridge.register("send_market", lambda **_: {"retcode": retcode, "comment": "x"})
        out = om.execute(make_decision(), "tag002", gold(), T0)
        assert not out.ok
        sends = sum(1 for m, _ in bridge.calls if m == "send_market")
        assert sends == 1, "a terminal rejection must not be retried"

    def test_requote_is_retried_with_a_fresh_price(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        calls = {"n": 0}

        def flaky(**_):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return {"retcode": rc.REQUOTE, "comment": "requote"}
            return {
                "retcode": rc.DONE,
                "comment": "done",
                "order": 556,
                "deal": 1,
                "volume": 0.1,
                "price": 2000.12,
            }

        bridge.register("send_market", flaky)
        out = om.execute(make_decision(), "tag003", gold(), T0)
        assert out.ok and out.attempts == 2

    def test_unknown_retcode_trips_the_kill_switch(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        bridge.register("send_market", lambda **_: {"retcode": 45678, "comment": "???"})
        out = om.execute(make_decision(), "tag004", gold(), T0)
        assert not out.ok
        assert ks.active


class TestAmbiguousSend:
    """The most dangerous case in the system."""

    def test_never_resends_after_an_ambiguous_failure(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        om.settings = Settings(execution=ExecutionConfig(reconcile_timeout_seconds=2.0))
        bridge.drop_methods.add("send_market")
        out = om.execute(make_decision(), "tag005", gold(), T0)
        sends = sum(1 for m, _ in bridge.calls if m == "send_market")
        assert sends == 1, "an ambiguous send must NEVER be resent"
        assert out.status is OrderStatus.RECONCILING

    def test_recovers_when_the_order_actually_landed(self, manager) -> None:  # type: ignore[no-untyped-def]
        """The realistic bad case: the order reached the server, the response did not.

        The position must appear only AFTER the send, otherwise the duplicate guard
        fires first and the reconciliation path is never exercised.
        """
        om, broker, ks, bridge = manager
        om.settings = Settings(execution=ExecutionConfig(reconcile_timeout_seconds=5.0))
        bridge.drop_methods.add("send_market")
        bridge.on_drop["send_market"] = lambda params: bridge.positions.append(
            {
                "ticket": 901,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.1,
                "sl": 1990.0,
                "tp": 2025.0,
                "time": time.time(),
                "magic": 777,
                "comment": "sweep_mss_fv:tag006",
                "profit": 0.0,
                "swap": 0.0,
            }
        )
        out = om.execute(make_decision(), "tag006", gold(), T0)
        assert out.ok and out.reconciled and out.ticket == 901
        assert not ks.active

    def test_unresolvable_state_halts_trading(self, manager) -> None:  # type: ignore[no-untyped-def]
        """An unknown order state is a stop-everything condition, not a retry."""
        om, broker, ks, bridge = manager
        om.settings = Settings(execution=ExecutionConfig(reconcile_timeout_seconds=2.0))
        bridge.drop_methods.add("send_market")
        out = om.execute(make_decision(), "tag007", gold(), T0)
        assert not out.ok
        assert ks.is_active(KillSwitchReason.STATE_DIVERGENCE)

    def test_startup_reconciliation_resolves_leftovers(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        bridge.positions = [
            {
                "ticket": 902,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.1,
                "sl": 1990.0,
                "tp": 0.0,
                "time": time.time(),
                "magic": 777,
                "comment": "s:alive",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        out = om.reconcile_unresolved(["alive", "ghost"], magic=777)
        assert "FILLED" in out["alive"]
        assert "NOT_FOUND" in out["ghost"]


class TestStopWidening:
    def _mp(self) -> ManagedPosition:
        return ManagedPosition(
            ticket=1,
            plan=make_decision().plan,
            entry=2000.0,
            initial_stop=1990.0,
            current_stop=1990.0,
            take_profit=2020.0,
            volume=0.1,
            remaining=0.1,
            opened_at=T0,
            direction=Direction.LONG,
        )

    def test_widening_a_stop_raises(self, manager) -> None:  # type: ignore[no-untyped-def]
        """There is no code path in PositionManager that can widen a stop."""
        om, broker, ks, bridge = manager
        pm = PositionManager(broker, Settings())
        mp = self._mp()
        with pytest.raises(StopWideningRefused):
            pm.modify_stop(mp, 1980.0, "widen")
        assert mp.current_stop == 1990.0

    def test_tightening_is_allowed(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        pm = PositionManager(broker, Settings())
        mp = self._mp()
        action = pm.modify_stop(mp, 1995.0, "break_even: 1.0R")
        assert action.applied and mp.current_stop == 1995.0

    def test_short_side_widening_also_refused(self, manager) -> None:  # type: ignore[no-untyped-def]
        om, broker, ks, bridge = manager
        pm = PositionManager(broker, Settings())
        mp = self._mp()
        mp.direction = Direction.SHORT
        mp.entry, mp.initial_stop, mp.current_stop = 2000.0, 2010.0, 2010.0
        with pytest.raises(StopWideningRefused):
            pm.modify_stop(mp, 2020.0, "widen")


class TestReconciler:
    def _rec(self, bridge) -> tuple[Reconciler, KillSwitch]:  # type: ignore[no-untyped-def]
        broker = Mt5Broker(f"127.0.0.1:{bridge.port}", magic=777)
        ks = KillSwitch()
        return Reconciler(broker, ks, Notifier(), magic=777), ks

    def test_clean_when_states_match(self, bridge) -> None:  # type: ignore[no-untyped-def]
        rec, ks = self._rec(bridge)
        bridge.positions = [
            {
                "ticket": 1,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.0,
                "sl": 1990.0,
                "tp": 2020.0,
                "time": time.time(),
                "magic": 777,
                "comment": "s:t",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        result = rec.reconcile(
            [{"mt5_position": 1, "current_sl": 1990.0, "volume": 0.1, "remaining_volume": 0.1}]
        )
        assert result.clean and not ks.active

    def test_orphan_at_broker_is_adopted(self, bridge) -> None:  # type: ignore[no-untyped-def]
        """Crash recovery: a position carrying our magic that the DB lost."""
        rec, ks = self._rec(bridge)
        bridge.positions = [
            {
                "ticket": 2,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.0,
                "sl": 1990.0,
                "tp": 0.0,
                "time": time.time(),
                "magic": 777,
                "comment": "s:t",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        result = rec.reconcile([])
        assert 2 in result.adopted
        assert not ks.active

    def test_untagged_position_is_critical(self, bridge) -> None:  # type: ignore[no-untyped-def]
        """A human trading the same account invalidates every exposure calculation."""
        rec, ks = self._rec(bridge)
        bridge.positions = [
            {
                "ticket": 3,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 1.0,
                "price_open": 2000.0,
                "sl": 0.0,
                "tp": 0.0,
                "time": time.time(),
                "magic": 0,
                "comment": "manual",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        result = rec.reconcile([])
        assert result.critical
        assert ks.is_active(KillSwitchReason.STATE_DIVERGENCE)

    def test_position_closed_externally_is_closed_out(self, bridge) -> None:  # type: ignore[no-untyped-def]
        rec, ks = self._rec(bridge)
        bridge.positions = []
        result = rec.reconcile(
            [{"mt5_position": 5, "current_sl": 1990.0, "volume": 0.1, "remaining_volume": 0.1}]
        )
        assert 5 in result.closed_out
        assert not ks.active

    def test_missing_server_stop_is_critical(self, bridge) -> None:  # type: ignore[no-untyped-def]
        rec, ks = self._rec(bridge)
        bridge.positions = [
            {
                "ticket": 6,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.1,
                "price_open": 2000.0,
                "sl": 0.0,
                "tp": 0.0,
                "time": time.time(),
                "magic": 777,
                "comment": "s:t",
                "profit": 0.0,
                "swap": 0.0,
            }
        ]
        result = rec.reconcile(
            [{"mt5_position": 6, "current_sl": 1990.0, "volume": 0.1, "remaining_volume": 0.1}]
        )
        assert any(d.kind == "NO_SERVER_STOP" for d in result.critical)

    def test_broker_unreachable_halts(self, bridge) -> None:  # type: ignore[no-untyped-def]
        broker = Mt5Broker("127.0.0.1:1", magic=777)
        ks = KillSwitch()
        rec = Reconciler(broker, ks, Notifier(), magic=777)
        result = rec.reconcile([])
        assert not result.clean
        assert ks.is_active(KillSwitchReason.BROKER_UNREACHABLE)
