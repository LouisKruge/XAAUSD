"""The live orchestrator: does it actually start, and does a cycle produce a record?

Startup order matters more than anything else here. Before the engine may trade it must
acquire a single-instance lock (two engines against one account means duplicate
positions and double risk) and reconcile every unresolved order against the broker
(never assume a pre-crash view of positions is still true).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.synthetic import market
from xauusd.config.settings import Settings
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import KillSwitchReason, Mode, Timeframe
from xauusd.domain.types import SymbolSpec
from xauusd.engine.orchestrator import SingleInstanceLock, TradingEngine
from xauusd.execution.sim_broker import SimBroker
from xauusd.monitoring.alerts import Notifier


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


@pytest.fixture
def engine(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)  # so the file lock lands in a temp dir
    data = market(8000, seed=3)
    broker = SimBroker(gold_spec(), 10_000.0)
    for tf, s in data.items():
        broker.set_bars(tf, s.to_bars())
    broker.set_time(datetime.now(UTC), data[Timeframe.M5].last, 22)

    db = Database(f"sqlite:///{tmp_path}/engine.db")
    db.create_all()
    eng = TradingEngine(Settings(), broker, db, Notifier(), git_sha="test")
    yield eng
    eng.lock.release()


class TestStartup:
    def test_engine_starts_and_resolves_the_symbol(self, engine) -> None:  # type: ignore[no-untyped-def]
        assert engine.startup()
        assert engine.symbol == "XAUUSD"
        assert engine.spec_hash  # a spec hash is recorded for change detection
        assert engine.spec.contract_size == 100.0

    def test_startup_records_the_config_version(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Every decision references a config hash; the config itself must be stored."""
        assert engine.startup()
        with engine.db.session() as s:
            from xauusd.database.models import ConfigVersionRow

            rows = s.query(ConfigVersionRow).all()
            assert len(rows) == 1
            assert rows[0].config_hash == engine.settings.config_hash()

    def test_health_is_reported_after_startup(self, engine) -> None:  # type: ignore[no-untyped-def]
        assert engine.startup()
        health = engine.health.all()
        assert health["broker"].is_ok
        assert health["database"].is_ok


class TestSingleInstanceLock:
    def test_a_second_engine_is_refused(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Two engines against one account is the worst possible failure."""
        monkeypatch.chdir(tmp_path)
        db = Database(f"sqlite:///{tmp_path}/lock.db")
        db.create_all()
        first = SingleInstanceLock(db)
        second = SingleInstanceLock(db)
        assert first.acquire()
        assert not second.acquire(), "a second instance must be refused the lock"
        first.release()
        assert second.acquire(), "the lock must be reusable once released"
        second.release()

    def test_a_stale_lock_from_a_crash_is_reclaimed(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A crashed engine leaves a lock file; a restart must not be blocked forever."""
        monkeypatch.chdir(tmp_path)
        db = Database(f"sqlite:///{tmp_path}/lock.db")
        db.create_all()
        stale = tmp_path / "data" / "engine.lock"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("999999")  # a pid that does not exist
        assert SingleInstanceLock(db).acquire()


class TestDecisionCycle:
    def test_a_cycle_journals_a_decision_and_a_snapshot(self, engine) -> None:  # type: ignore[no-untyped-def]
        assert engine.startup()
        engine._refresh_context()
        engine._decision_cycle()

        with engine.db.session() as s:
            repos = Repositories(s)
            decisions = repos.decisions.recent(limit=10)
            assert decisions, "a cycle must journal a decision even when it does not trade"
            assert repos.snapshots.latest("XAUUSD") is not None
            # Every refusal names what blocked it.
            for d in decisions:
                if d.classification == "NO_TRADE":
                    assert d.blocking_gate or d.reasons_against

    def test_stale_bars_block_trading(self, engine) -> None:  # type: ignore[no-untyped-def]
        """The fixture's bars are historical while the clock is live, so the freshness
        gate must fire. Trading on stale data is exactly what it exists to prevent."""
        assert engine.startup()
        engine._refresh_context()
        engine._decision_cycle()
        with engine.db.session() as s:
            blockers = {d.blocking_gate for d in Repositories(s).decisions.recent(limit=10)}
        assert "data_freshness" in blockers

    def test_context_refresh_degrades_rather_than_raising(self, engine) -> None:  # type: ignore[no-untyped-def]
        """With no news feed configured, risk must floor at MODERATE, never LOW."""
        assert engine.startup()
        engine._refresh_context()
        assert engine.context.news is not None
        assert engine.context.news.risk.level >= 1  # MODERATE or worse


class TestSpecChangeHalts:
    def test_a_changed_symbol_spec_trips_the_kill_switch(self, engine) -> None:  # type: ignore[no-untyped-def]
        """A changed contract invalidates every open position's risk calculation."""
        assert engine.startup()
        engine.spec_hash = "deliberately-wrong"
        engine._decision_cycle()
        assert engine.kill_switch.is_active(KillSwitchReason.SPEC_CHANGED)


class TestLiveArmingIsEnforced:
    def test_live_mode_without_an_arming_file_refuses_to_start(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The config flag alone must never be enough to arm live trading."""
        monkeypatch.chdir(tmp_path)
        data = market(8000, seed=3)
        broker = SimBroker(gold_spec(), 10_000.0)
        for tf, s in data.items():
            broker.set_bars(tf, s.to_bars())
        broker.set_time(datetime.now(UTC), data[Timeframe.M5].last, 22)

        db = Database(f"sqlite:///{tmp_path}/live.db")
        db.create_all()
        settings = Settings(
            mode=Mode.LIVE,
            live_trading=True,
            live_arming_file=str(tmp_path / "absent.json"),
        )
        eng = TradingEngine(settings, broker, db, Notifier(), git_sha="test")
        try:
            assert not eng.startup(), "live mode must refuse to start unarmed"
        finally:
            eng.lock.release()
