"""The dashboard's control surface: who may reach it, and what happens when they do.

Two separate claims are tested here, and both were false before this suite existed.

1. The dashboard cannot be exposed without a token. It can halt the engine and close
   every open position, so "remember to set a token" is not a control — the bind is
   refused instead.

2. HALT and FLATTEN actually reach the broker. They used to be appended to a list in
   the API process that nothing ever read, which is worse than having no button: an
   operator hits FLATTEN in an emergency and is told it is queued.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.synthetic import market
from xauusd.config.settings import DashboardConfig, Settings
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Direction, KillSwitchReason, Timeframe
from xauusd.domain.types import OrderRequest, SymbolSpec
from xauusd.engine.orchestrator import TradingEngine
from xauusd.execution.sim_broker import SimBroker
from xauusd.monitoring.alerts import Notifier

TOKEN = "t" * 32


def gold_spec() -> SymbolSpec:
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


class TestABindWithoutATokenIsRefused:
    def test_loopback_needs_no_token(self) -> None:
        """On loopback the OS is the boundary, so a token is optional."""
        assert DashboardConfig().is_loopback
        assert DashboardConfig(host="127.0.0.1").auth_token is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.4"])
    def test_a_public_bind_without_a_token_is_refused(self, host: str) -> None:
        with pytest.raises(ValueError, match="not loopback"):
            DashboardConfig(host=host)

    def test_a_public_bind_with_a_token_is_allowed(self) -> None:
        assert DashboardConfig(host="0.0.0.0", auth_token=TOKEN).port == 8000

    def test_a_short_token_is_refused(self) -> None:
        """A four-character token on a public bind is theatre, not access control."""
        with pytest.raises(ValueError, match="at least 16"):
            DashboardConfig(host="0.0.0.0", auth_token="abc")

    def test_the_cli_flag_cannot_route_around_the_config(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """--host 0.0.0.0 never passes through DashboardConfig, so run() re-checks."""
        from xauusd.dashboard import api

        monkeypatch.setattr(api, "get_settings", lambda: Settings())
        with pytest.raises(SystemExit, match="refusing to bind"):
            api.run(host="0.0.0.0")


@pytest.fixture
def client(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from xauusd.dashboard import api

    settings = Settings(
        database={"url": f"sqlite:///{tmp_path}/dash.db"},
        dashboard={"host": "0.0.0.0", "auth_token": TOKEN},
    )
    monkeypatch.setattr(api, "_settings", settings)
    monkeypatch.setattr(api, "_db", None)
    yield TestClient(api.app)
    monkeypatch.setattr(api, "_db", None)


class TestEveryApiPathIsGuarded:
    def test_reads_require_the_token(self, client) -> None:  # type: ignore[no-untyped-def]
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/decisions").status_code == 401

    def test_writes_require_the_token(self, client) -> None:  # type: ignore[no-untyped-def]
        body = {"reason": "unauthorised", "operator": "attacker"}
        assert client.post("/api/commands/flatten", json=body).status_code == 401
        assert client.post("/api/commands/halt", json=body).status_code == 401

    def test_a_wrong_token_is_refused(self, client) -> None:  # type: ignore[no-untyped-def]
        r = client.get("/api/health", headers={"Authorization": f"Bearer {'x' * 32}"})
        assert r.status_code == 401

    def test_the_right_token_is_accepted(self, client) -> None:  # type: ignore[no-untyped-def]
        r = client.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200

    def test_an_unauthenticated_read_cannot_drain_the_command_queue(self, client) -> None:  # type: ignore[no-untyped-def]
        """The old GET /api/commands/pending returned AND cleared the queue, so an
        unauthenticated poll could swallow an operator's emergency stop."""
        auth = {"Authorization": f"Bearer {TOKEN}"}
        client.post("/api/commands/flatten", json={"reason": "real emergency"}, headers=auth)
        assert client.get("/api/commands").status_code == 401
        # The command is still there for the engine.
        rows = client.get("/api/commands", headers=auth).json()
        assert [r["command"] for r in rows] == ["FLATTEN"]
        assert rows[0]["status"] == "QUEUED"

    def test_the_page_itself_loads_without_a_token(self, client) -> None:  # type: ignore[no-untyped-def]
        """A browser cannot set a header on a top-level navigation; the shell holds no
        data, and every /api call it then makes is guarded."""
        assert client.get("/").status_code == 200


@pytest.fixture
def engine(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    data = market(8000, seed=3)
    broker = SimBroker(gold_spec(), 10_000.0)
    for tf, s in data.items():
        broker.set_bars(tf, s.to_bars())
    broker.set_time(datetime.now(UTC), data[Timeframe.M5].last, 22)

    db = Database(f"sqlite:///{tmp_path}/engine.db")
    db.create_all()
    eng = TradingEngine(Settings(), broker, db, Notifier(), git_sha="test")
    assert eng.startup()
    yield eng
    eng.lock.release()


def open_position(engine: TradingEngine, tag: str) -> None:
    quote = engine.broker.quote("XAUUSD")
    engine.broker.send_market(
        OrderRequest(
            symbol="XAUUSD",
            direction=Direction.LONG,
            volume=0.10,
            price=quote.ask,
            stop_loss=quote.bid - 10.0,
            take_profit=quote.bid + 20.0,
            client_tag=tag,
            magic=engine.settings.broker.magic,
            comment=f"test:{tag}",
        )
    )


def queue(db: Database, command: str, reason: str = "test") -> int:
    with db.session() as s:
        return Repositories(s).commands.queue(command, reason, "operator")


class TestCommandsReachTheBroker:
    def test_halt_trips_the_kill_switch(self, engine) -> None:  # type: ignore[no-untyped-def]
        queue(engine.db, "HALT", "stepping away from the desk")
        assert engine._drain_commands() == 1
        assert engine.kill_switch.is_active(KillSwitchReason.MANUAL)

    def test_flatten_closes_every_open_position(self, engine) -> None:  # type: ignore[no-untyped-def]
        for i in range(2):
            open_position(engine, f"t{i}")
        assert len(engine.broker.positions()) == 2

        queue(engine.db, "FLATTEN", "closing the book")
        assert engine._drain_commands() == 1
        assert engine.broker.positions() == [], "every position must be closed"

    def test_flatten_also_halts(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Flattening without halting invites re-entry on the next M5 close, which is
        the opposite of what the operator asked for."""
        queue(engine.db, "FLATTEN", "closing the book")
        engine._drain_commands()
        assert engine.kill_switch.is_active(KillSwitchReason.MANUAL)

    def test_a_command_is_executed_once_even_across_restarts(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Claiming is what stops a redelivery closing a position twice."""
        queue(engine.db, "HALT")
        assert engine._drain_commands() == 1
        assert engine._drain_commands() == 0, "a claimed command must not run again"

    def test_the_outcome_is_recorded_against_the_request(self, engine) -> None:  # type: ignore[no-untyped-def]
        """An instruction to close every position needs a permanent record of who asked
        and what happened."""
        queue(engine.db, "HALT", "documented reason")
        engine._drain_commands()
        with engine.db.session() as s:
            row = Repositories(s).commands.recent(1)[0]
        assert row.status == "DONE"
        assert row.operator == "operator"
        assert row.reason == "documented reason"
        assert row.completed_at is not None
        assert "kill switch" in (row.result or "")

    def test_a_command_interrupted_by_a_crash_is_not_dropped(self, engine) -> None:  # type: ignore[no-untyped-def]
        """A crash between claiming and executing must not strand an emergency stop.

        Both commands are idempotent, so re-running one on restart is strictly safer
        than silently losing it.
        """
        queue(engine.db, "FLATTEN", "emergency")
        with engine.db.session() as s:
            claimed = Repositories(s).commands.claim_pending()
            assert [r.status for r in claimed] == ["CLAIMED"]
        # ... engine dies here, before _execute_command ran.

        with engine.db.session() as s:
            assert Repositories(s).commands.requeue_stale_claims()
        assert engine._drain_commands() == 1, "the command must run after the restart"
        assert engine.kill_switch.is_active(KillSwitchReason.MANUAL)

    def test_a_failed_flatten_is_recorded_as_failed(self, engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The operator must never be told the account is flat when it is not."""
        open_position(engine, "stuck")

        def refuse(ticket: int, volume: float | None = None):  # type: ignore[no-untyped-def]
            raise RuntimeError("broker rejected the close")

        monkeypatch.setattr(engine.broker, "close_position", refuse)
        queue(engine.db, "FLATTEN", "will not work")
        engine._drain_commands()

        with engine.db.session() as s:
            row = Repositories(s).commands.recent(1)[0]
        assert row.status == "FAILED"
        assert "failed to close" in (row.result or "")
