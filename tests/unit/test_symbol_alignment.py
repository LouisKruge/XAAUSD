"""One symbol, everywhere.

This broker calls spot gold `GOLD`. Discovery finds it at runtime, but the OFFLINE
paths — backtest, validation, anything reading harvested history — have no broker to
ask and fall back to the configured name. When those two disagree, `harvest` writes
under GOLD, the backtest reads under XAUUSD, and a full database reports "0 bars".

That is the same divergence that broke `doctor` twice: a consumer using a different
code path from the producer. These tests pin the resolution so it cannot recur, and
assert the substitution is always announced rather than silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xauusd.config.settings import load_settings
from xauusd.data.harvest import resolve_stored_symbol, stored_symbols
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar


@pytest.fixture
def db(tmp_path):  # type: ignore[no-untyped-def]
    d = Database(f"sqlite:///{tmp_path}/sym.db")
    d.create_all()
    return d


def store(db: Database, symbol: str, n: int, tf: Timeframe = Timeframe.M5) -> None:
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    bars = [
        Bar(
            t0 + timedelta(seconds=tf.seconds * i),
            2600.0,
            2601.0,
            2599.0,
            2600.5,
            tick_volume=10,
            spread_points=25,
        )
        for i in range(n)
    ]
    with db.session() as s:
        Repositories(s).bars.upsert_many(symbol, tf, bars, source="mt5")
        s.commit()


class TestTheConfiguredSymbolMatchesTheBroker:
    def test_config_names_the_brokers_symbol(self) -> None:
        """The terminal shows "GOLD, M1: SPOT Gold Ounce vs US Dollar"."""
        assert load_settings().symbol == "GOLD"

    def test_discovery_still_accepts_either_name(self) -> None:
        """Naming GOLD must not break a broker that calls it XAUUSD — discovery is
        still the runtime authority."""
        patterns = load_settings().data.symbol_patterns
        assert r"^XAU" in patterns and r"^GOLD" in patterns

    def test_m1_is_loaded_for_the_scalp_engine(self) -> None:
        assert load_settings().data.bars_to_load.get("M1", 0) >= 1500

    def test_the_analyzer_reads_m1(self) -> None:
        """MicroAnalyzer starves without it, and reports 'not usable' forever — which
        looks exactly like a market with no setups."""
        from xauusd.core.analyzer import LTF

        assert Timeframe.M1 in LTF


class TestHistoryIsReadUnderTheSymbolItWasStoredWith:
    def test_the_configured_symbol_wins_when_it_has_bars(self, db) -> None:  # type: ignore[no-untyped-def]
        store(db, "GOLD", 50)
        symbol, note = resolve_stored_symbol(db, "GOLD", Timeframe.M5)
        assert symbol == "GOLD"
        assert note == "", "no substitution, so nothing to announce"

    def test_a_single_other_symbol_is_used_and_announced(self, db) -> None:  # type: ignore[no-untyped-def]
        """The exact failure: harvested as GOLD, configured as XAUUSD."""
        store(db, "GOLD", 50)
        symbol, note = resolve_stored_symbol(db, "XAUUSD", Timeframe.M5)
        assert symbol == "GOLD"
        assert "no M5 history under 'XAUUSD'" in note
        assert "GOLD" in note and "50 bars" in note

    def test_an_empty_database_returns_the_configured_name(self, db) -> None:  # type: ignore[no-untyped-def]
        """Nothing to disambiguate; the caller's own bar-count check reports it."""
        symbol, note = resolve_stored_symbol(db, "GOLD", Timeframe.M5)
        assert symbol == "GOLD"
        assert note == ""

    def test_several_candidate_symbols_refuse_to_guess(self, db) -> None:  # type: ignore[no-untyped-def]
        """Picking one silently could backtest the wrong instrument."""
        store(db, "GOLD", 50)
        store(db, "XAUUSD.pro", 30)
        with pytest.raises(SystemExit, match="held under several symbols"):
            resolve_stored_symbol(db, "XAUUSD", Timeframe.M5)

    def test_timeframes_are_counted_separately(self, db) -> None:  # type: ignore[no-untyped-def]
        """M1 held but M5 empty is a real state, and must not resolve across them."""
        store(db, "GOLD", 40, Timeframe.M1)
        assert stored_symbols(db, Timeframe.M1) == {"GOLD": 40}
        assert stored_symbols(db, Timeframe.M5) == {}
