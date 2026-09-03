"""Harvesting real history into the database.

Every backtest an operator could previously run used `--synthetic`, and the pipeline
suite asserts synthetic data produces no trades. So "0 trades" was reporting the
absence of data, not the absence of edge. These tests pin the two properties that make
harvested history trustworthy enough to backtest against: nothing is invented, and
nothing still forming is written down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.synthetic import market
from xauusd.data.harvest import coverage, harvest
from xauusd.database.session import Database
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import SymbolSpec
from xauusd.execution.sim_broker import SimBroker

TF = Timeframe.M5


def gold_spec() -> SymbolSpec:
    return SymbolSpec("XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)


@pytest.fixture
def db(tmp_path):  # type: ignore[no-untyped-def]
    d = Database(f"sqlite:///{tmp_path}/harvest.db")
    d.create_all()
    return d


@pytest.fixture
def broker():  # type: ignore[no-untyped-def]
    data = market(4000, seed=11)
    b = SimBroker(gold_spec(), 10_000.0)
    for tf, s in data.items():
        b.set_bars(tf, s.to_bars())
    bars = data[TF].to_bars()
    # "Now" is one second into the bar after the last closed one, so the most recent
    # bar the broker will serve is genuinely complete.
    b.set_time(bars[-1].ts + timedelta(seconds=TF.seconds), bars[-1], 22)
    return b, bars


class TestHarvestedHistoryIsUsable:
    def test_bars_reach_the_database(self, db, broker) -> None:  # type: ignore[no-untyped-def]
        b, bars = broker
        report = harvest(b, db, "XAUUSD", TF, wanted=500, chunk=200)
        assert report.added == 500
        count, earliest, latest = coverage(db, "XAUUSD", TF)
        assert count == 500
        assert latest == bars[-1].ts, "the newest closed bar must be the newest stored"

    def test_it_walks_back_across_several_chunks(self, db, broker) -> None:  # type: ignore[no-untyped-def]
        """A single copy_rates call cannot span a useful backtest, so the walk backwards
        is the whole mechanism — and an off-by-one there silently truncates history."""
        b, _ = broker
        report = harvest(b, db, "XAUUSD", TF, wanted=1000, chunk=250)
        assert report.fetched == 1000
        count, earliest, latest = coverage(db, "XAUUSD", TF)
        assert count == 1000
        # Contiguous: 1000 M5 bars span exactly 999 intervals.
        assert latest - earliest == timedelta(seconds=TF.seconds * 999)

    def test_running_it_twice_adds_nothing(self, db, broker) -> None:  # type: ignore[no-untyped-def]
        """An operator will press the button again. That must be free, not corrupting."""
        b, _ = broker
        harvest(b, db, "XAUUSD", TF, wanted=400, chunk=200)
        second = harvest(b, db, "XAUUSD", TF, wanted=400, chunk=200)
        assert second.added == 0
        assert second.duplicates == 400
        assert coverage(db, "XAUUSD", TF)[0] == 400


class TestNothingIsInvented:
    def test_running_out_of_history_is_reported_not_padded(self, db, broker) -> None:  # type: ignore[no-untyped-def]
        b, bars = broker
        report = harvest(b, db, "XAUUSD", TF, wanted=99_000, chunk=1000)
        assert report.exhausted and report.short
        assert report.fetched == len(bars), "every available bar, and not one more"
        assert coverage(db, "XAUUSD", TF)[0] == len(bars)
        assert "all the history this account can see" in report.summary()

    def test_a_broker_with_no_history_terminates(self, db) -> None:  # type: ignore[no-untyped-def]
        """An empty answer must end the walk, not spin against it."""
        b = SimBroker(gold_spec(), 10_000.0)
        b.set_time(datetime.now(UTC), None, 22)
        report = harvest(b, db, "XAUUSD", TF, wanted=10_000)
        assert report.fetched == 0 and report.exhausted
        assert "no bars available" in report.summary()


class TestTheFormingBarIsNeverStored:
    """copy_rates_from returns the current, incomplete bar. Written to the database it
    is indistinguishable from a closed one, so its partial high/low would be read as
    final by resampling, ATR and structure — a look-ahead defect saved permanently."""

    def test_an_open_bar_is_dropped(self, db, broker) -> None:  # type: ignore[no-untyped-def]
        b, bars = broker
        # Move time into the middle of the bar following the last complete one.
        b.set_time(bars[-1].ts + timedelta(seconds=TF.seconds + 60), bars[-1], 22)
        # Ask the broker directly for a window that includes the forming bar.
        forming_end = bars[-1].ts + timedelta(seconds=TF.seconds * 2)
        report = harvest(
            b, db, "XAUUSD", TF, wanted=100, chunk=100, now=forming_end - timedelta(seconds=60)
        )
        _, _, latest = coverage(db, "XAUUSD", TF)
        assert latest is not None
        assert latest + timedelta(seconds=TF.seconds) <= forming_end - timedelta(seconds=60), (
            "a bar whose close time has not passed must not be stored"
        )
        assert report.added > 0
