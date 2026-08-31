"""Position management after entry.

The invariant under test throughout: a stop may be tightened, never widened, and a
position may never persist without a server-side stop.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.fake_bridge import FakeBridge, default_account, default_spec
from xauusd.config.settings import ExecutionConfig, Settings
from xauusd.domain.enums import Direction, Timeframe
from xauusd.domain.types import (
    BrokerPosition,
    Quote,
    SymbolSpec,
    TargetLevel,
    TradePlan,
)
from xauusd.execution import retcodes as rc
from xauusd.execution.mt5_broker import Mt5Broker
from xauusd.execution.position_manager import PositionManager, StopWideningRefused

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


def plan() -> TradePlan:
    return TradePlan(
        "sweep_mss_fvg",
        "1.0",
        Direction.LONG,
        2000.0,
        1990.0,
        (TargetLevel(2025.0, 2.5, "PDH"),),
        T0,
        Timeframe.M15,
        "close below 1990",
        symbol="XAUUSD",
    )


@pytest.fixture
def bridge():
    b = FakeBridge()
    b.register("account", lambda **_: default_account())
    b.register("symbol_spec", lambda symbol, **_: default_spec(symbol))
    b.register("quote", lambda symbol, **_: {"ts": time.time(), "bid": 2010.0, "ask": 2010.2})
    b.register("modify_position", lambda **_: {"retcode": rc.DONE, "comment": "ok"})
    b.register(
        "close_position",
        lambda **_: {"retcode": rc.DONE, "comment": "ok", "price": 2010.0, "volume": 0.1},
    )
    b.start()
    yield b
    b.stop()


@pytest.fixture
def pm(bridge):  # type: ignore[no-untyped-def]
    broker = Mt5Broker(f"127.0.0.1:{bridge.port}", magic=777)
    settings = Settings(
        execution=ExecutionConfig(
            break_even_at_r=1.0,
            break_even_offset_r=0.05,
            trail_enabled=True,
            trail_activate_r=1.5,
            time_stop_bars=10,
            time_stop_min_r=0.3,
            flat_before_weekend=False,
        )
    )
    manager = PositionManager(broker, settings)
    pos = BrokerPosition(
        1, "XAUUSD", Direction.LONG, 0.1, 2000.0, 1990.0, 2025.0, T0, magic=777, comment="s:tag"
    )
    mp = manager.adopt(1, plan(), pos)
    return manager, mp, bridge


class TestBreakEven:
    def test_moves_to_break_even_at_the_configured_r(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        quote = Quote(T0, 2010.0, 2010.2)  # +1.0R on a 10-point stop
        actions = manager.manage(quote, T0, gold())
        assert mp.moved_to_be
        assert mp.current_stop > mp.entry
        assert any(a.applied and "break_even" in a.reason for a in actions)

    def test_does_not_move_before_the_threshold(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        manager.manage(Quote(T0, 2004.0, 2004.2), T0, gold())  # +0.4R
        assert not mp.moved_to_be
        assert mp.current_stop == 1990.0


class TestTrailing:
    def test_trails_behind_price_once_activated(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, bridge = pm
        bridge.register(
            "quote", lambda symbol, **_: {"ts": time.time(), "bid": 2020.0, "ask": 2020.2}
        )
        manager.manage(Quote(T0, 2020.0, 2020.2), T0, gold())  # +2.0R
        assert mp.current_stop > mp.entry

    def test_a_trail_never_moves_a_stop_backwards(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, bridge = pm
        bridge.register(
            "quote", lambda symbol, **_: {"ts": time.time(), "bid": 2020.0, "ask": 2020.2}
        )
        manager.manage(Quote(T0, 2020.0, 2020.2), T0, gold())
        high_water = mp.current_stop
        # price retraces; the stop must hold
        manager.manage(Quote(T0, 2012.0, 2012.2), T0 + timedelta(minutes=5), gold())
        assert mp.current_stop >= high_water


class TestStopWidening:
    def test_direct_widening_raises(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        with pytest.raises(StopWideningRefused):
            manager.modify_stop(mp, 1980.0, "widen")
        assert mp.current_stop == 1990.0

    def test_no_management_path_can_widen_a_stop(self, pm) -> None:  # type: ignore[no-untyped-def]
        """Walk price up and back down repeatedly; the stop must be monotonic."""
        manager, mp, bridge = pm
        stops = []
        for price in (2005, 2012, 2020, 2014, 2025, 2008, 2030, 2001):
            bridge.register(
                "quote",
                lambda symbol, p=price, **_: {
                    "ts": time.time(),
                    "bid": float(p),
                    "ask": float(p) + 0.2,
                },
            )
            manager.manage(
                Quote(T0, float(price), float(price) + 0.2), T0 + timedelta(minutes=5), gold()
            )
            if mp.ticket in manager.positions:
                stops.append(mp.current_stop)
        assert stops == sorted(stops), f"stop moved backwards: {stops}"


class TestTimeStop:
    def test_closes_a_position_that_has_not_worked(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        mp.bars_held = 20  # past time_stop_bars
        actions = manager.manage(Quote(T0, 2001.0, 2001.2), T0, gold(), bar_closed=True)
        assert any(a.kind == "CLOSE" and "TIME_STOP" in a.reason for a in actions)
        assert mp.ticket not in manager.positions

    def test_leaves_a_working_position_alone(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        mp.bars_held = 20
        actions = manager.manage(Quote(T0, 2015.0, 2015.2), T0, gold(), bar_closed=True)
        assert not any(a.kind == "CLOSE" for a in actions)
        assert mp.ticket in manager.positions


class TestServerSideStop:
    def test_a_missing_stop_is_restored(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        mp.current_stop = 0.0
        actions = manager.manage(Quote(T0, 2005.0, 2005.2), T0, gold())
        assert any(a.kind == "ATTACH_STOP" and a.applied for a in actions)
        assert mp.current_stop == 1990.0

    def test_a_position_that_cannot_be_protected_is_closed(self, pm) -> None:  # type: ignore[no-untyped-def]
        """Better flat than running naked."""
        manager, mp, bridge = pm
        mp.current_stop = 0.0
        bridge.register(
            "modify_position", lambda **_: {"retcode": rc.INVALID_STOPS, "comment": "no"}
        )
        actions = manager.manage(Quote(T0, 2005.0, 2005.2), T0, gold())
        assert any(a.kind == "CLOSE" for a in actions)


class TestRExcursions:
    def test_mae_and_mfe_are_tracked(self, pm) -> None:  # type: ignore[no-untyped-def]
        manager, mp, _ = pm
        manager.manage(Quote(T0, 1995.0, 1995.2), T0, gold())  # -0.5R
        manager.manage(Quote(T0, 2015.0, 2015.2), T0, gold())  # +1.5R
        assert mp.mae_r == pytest.approx(0.5, abs=0.05)
        assert mp.mfe_r == pytest.approx(1.5, abs=0.05)
