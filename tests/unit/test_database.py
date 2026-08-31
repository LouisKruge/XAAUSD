"""Database: the audit spine and the vintage anti-leak boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Classification, Direction, OrderStatus, Timeframe
from xauusd.domain.types import Bar, Decision, GateResult, TargetLevel, TradePlan

UTC = UTC


def D(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)  # type: ignore[arg-type]


class TestBarRepository:
    def test_insert_is_idempotent(self, db: Database, t0: datetime) -> None:
        bars = [Bar(t0 + timedelta(minutes=5 * i), 2000, 2005, 1995, 2002) for i in range(10)]
        with db.session() as s:
            assert Repositories(s).bars.upsert_many("XAUUSD", Timeframe.M5, bars) == 10
        with db.session() as s:
            assert Repositories(s).bars.upsert_many("XAUUSD", Timeframe.M5, bars) == 0

    def test_load_respects_end_cutoff(self, db: Database, t0: datetime) -> None:
        bars = [Bar(t0 + timedelta(minutes=5 * i), 2000, 2005, 1995, 2002) for i in range(10)]
        with db.session() as s:
            Repositories(s).bars.upsert_many("XAUUSD", Timeframe.M5, bars)
        with db.session() as s:
            got = Repositories(s).bars.load("XAUUSD", Timeframe.M5, end=t0 + timedelta(minutes=20))
            assert len(got) == 5
            assert all(b.ts <= t0 + timedelta(minutes=20) for b in got)

    def test_load_returns_chronological_order(self, db: Database, t0: datetime) -> None:
        bars = [Bar(t0 + timedelta(minutes=5 * i), 2000, 2005, 1995, 2002) for i in range(10)]
        with db.session() as s:
            Repositories(s).bars.upsert_many("XAUUSD", Timeframe.M5, bars)
        with db.session() as s:
            got = Repositories(s).bars.load("XAUUSD", Timeframe.M5, limit=5)
            assert [b.ts for b in got] == sorted(b.ts for b in got)

    def test_sources_are_not_mixed(self, db: Database, t0: datetime) -> None:
        """Broker and third-party history must never merge - they differ materially."""
        bars = [Bar(t0, 2000, 2005, 1995, 2002)]
        with db.session() as s:
            r = Repositories(s)
            r.bars.upsert_many("XAUUSD", Timeframe.M5, bars, source="mt5:broker")
            r.bars.upsert_many("XAUUSD", Timeframe.M5, bars, source="dukascopy")
        with db.session() as s:
            r = Repositories(s)
            assert r.bars.count("XAUUSD", Timeframe.M5, "mt5:broker") == 1
            assert r.bars.count("XAUUSD", Timeframe.M5, "dukascopy") == 1

    def test_gap_detection_ignores_weekends(self, db: Database) -> None:
        # Friday 20:00 -> Monday 00:00 is an expected weekend gap, not a data defect.
        fri = D(2026, 1, 2, 20, 0)
        mon = D(2026, 1, 5, 0, 0)
        with db.session() as s:
            Repositories(s).bars.upsert_many(
                "XAUUSD",
                Timeframe.H1,
                [Bar(fri, 1, 1, 1, 1), Bar(mon, 1, 1, 1, 1)],
            )
        with db.session() as s:
            assert Repositories(s).bars.find_gaps("XAUUSD", Timeframe.H1) == []

    def test_gap_detection_finds_real_gaps(self, db: Database) -> None:
        a, b = D(2026, 1, 5, 10, 0), D(2026, 1, 5, 15, 0)
        with db.session() as s:
            Repositories(s).bars.upsert_many(
                "XAUUSD", Timeframe.H1, [Bar(a, 1, 1, 1, 1), Bar(b, 1, 1, 1, 1)]
            )
        with db.session() as s:
            assert Repositories(s).bars.find_gaps("XAUUSD", Timeframe.H1) == [(a, b)]


class TestMacroVintage:
    """The single most important anti-leak rule in the whole system."""

    def test_a_value_is_invisible_before_its_release(self, db: Database) -> None:
        with db.session() as s:
            r = Repositories(s)
            r.macro.add_observation("DGS10", D(2026, 1, 1), D(2026, 1, 2), 4.20)
            s.flush()
            assert r.macro.value_as_of("DGS10", D(2026, 1, 1, 12)) is None
            assert r.macro.value_as_of("DGS10", D(2026, 1, 2, 1))[0] == pytest.approx(4.20)

    def test_a_revision_does_not_leak_backwards(self, db: Database) -> None:
        with db.session() as s:
            r = Repositories(s)
            r.macro.add_observation("CPI", D(2026, 1, 1), D(2026, 1, 10), 3.0)
            r.macro.add_observation("CPI", D(2026, 1, 1), D(2026, 3, 1), 3.4, revision=1)
            s.flush()
            # In January, only the original print existed.
            assert r.macro.value_as_of("CPI", D(2026, 1, 15))[0] == pytest.approx(3.0)
            # After the revision was published, the revised value is correct.
            assert r.macro.value_as_of("CPI", D(2026, 3, 15))[0] == pytest.approx(3.4)

    def test_series_as_of_is_point_in_time(self, db: Database) -> None:
        with db.session() as s:
            r = Repositories(s)
            for day in range(1, 20):
                r.macro.add_observation(
                    "DXY", D(2026, 1, day), D(2026, 1, day) + timedelta(hours=12), 100 + day
                )
            s.flush()
            series = r.macro.series_as_of("DXY", D(2026, 1, 10, 18))
            assert len(series) == 10
            assert max(d for d, _ in series) == D(2026, 1, 10)


class TestDecisionJournal:
    def _decision(self, ts: datetime, cls: Classification, gates: tuple) -> Decision:
        plan = TradePlan(
            "sweep_mss_fvg",
            "1.0",
            Direction.LONG,
            2000.0,
            1990.0,
            (TargetLevel(2020.0, 2.0, "PDH liquidity"),),
            ts,
            Timeframe.M15,
            "close below 1990 invalidates",
        )
        return Decision(
            ts=ts,
            symbol="XAUUSD",
            classification=cls,
            mode="BACKTEST",
            plan=plan,
            score=78.0,
            probability=0.61,
            gates=gates,
            features={"htf_bias": 1, "sweep_quality": 0.8},
        )

    def test_every_decision_is_journalled_with_its_blocker(
        self, db: Database, t0: datetime
    ) -> None:
        gates = (
            GateResult("spread", True, 22.0, 50.0),
            GateResult("min_rr", False, 1.6, 2.0, "target liquidity too close"),
            GateResult("news_risk", False, "HIGH", "MODERATE"),
        )
        with db.session() as s:
            did = Repositories(s).decisions.save(self._decision(t0, Classification.NO_TRADE, gates))
        with db.session() as s:
            row = Repositories(s).decisions.get(did)
            assert row is not None
            assert row.blocking_gate == "min_rr"  # FIRST failing gate
            assert set(row.all_blocking) == {"min_rr", "news_risk"}  # and all of them
            assert len(row.gate_trace) == 3
            assert row.features["sweep_quality"] == 0.8

    def test_rejection_ledger_answers_why_no_trades(self, db: Database, t0: datetime) -> None:
        with db.session() as s:
            r = Repositories(s)
            for i in range(5):
                r.decisions.save(
                    self._decision(
                        t0 + timedelta(minutes=i),
                        Classification.NO_TRADE,
                        (GateResult("news_blackout", False, True, False),),
                    )
                )
            for i in range(3):
                r.decisions.save(
                    self._decision(
                        t0 + timedelta(hours=1, minutes=i),
                        Classification.NO_TRADE,
                        (GateResult("min_rr", False, 1.8, 2.0),),
                    )
                )
            r.decisions.save(
                self._decision(
                    t0 + timedelta(hours=2), Classification.A, (GateResult("all", True),)
                )
            )
        with db.session() as s:
            ledger = Repositories(s).decisions.rejection_ledger(t0, t0 + timedelta(days=1))
            assert dict(ledger) == {"news_blackout": 5, "min_rr": 3}
            counts = Repositories(s).decisions.counts_by_classification(t0, t0 + timedelta(days=1))
            assert counts == {"NO_TRADE": 8, "A": 1}

    def test_explain_renders_both_questions(self, t0: datetime) -> None:
        d = self._decision(
            t0, Classification.NO_TRADE, (GateResult("min_rr", False, 1.6, 2.0, "too close"),)
        )
        text = d.explain()
        assert "BLOCKED BY" in text and "min_rr" in text and "1.6" in text
        d2 = self._decision(t0, Classification.A_PLUS, (GateResult("all", True),))
        assert "All gates passed" in d2.explain()


class TestOrderIdempotency:
    def test_client_tag_is_unique(self, db: Database) -> None:
        with db.session() as s:
            Repositories(s).orders.create_intent(
                "tag123", 1, "XAUUSD", Direction.LONG, 0.1, 2000, 1990, 2020
            )
        with pytest.raises(Exception), db.session() as s:
            Repositories(s).orders.create_intent(
                "tag123", 1, "XAUUSD", Direction.LONG, 0.1, 2000, 1990, 2020
            )

    def test_unresolved_orders_are_found_for_reconciliation(self, db: Database) -> None:
        with db.session() as s:
            r = Repositories(s)
            o1 = r.orders.create_intent("a", 1, "XAUUSD", Direction.LONG, 0.1, 2000, 1990, None)
            o2 = r.orders.create_intent("b", 1, "XAUUSD", Direction.LONG, 0.1, 2000, 1990, None)
            r.orders.update_status(o1, OrderStatus.FILLED)
            r.orders.update_status(o2, OrderStatus.RECONCILING)
        with db.session() as s:
            unresolved = Repositories(s).orders.unresolved()
            assert [o.client_tag for o in unresolved] == ["b"]


class TestStrategyStatusGate:
    def test_status_gates_live_routing(self, db: Database) -> None:
        with db.session() as s:
            r = Repositories(s)
            r.strategy_status.set_status("sweep_mss_fvg", "1.0", "DEV")
            r.strategy_status.set_status("sweep_mss_ob", "1.0", "OOS_PASSED", max_class="A_PLUS")
        with db.session() as s:
            r = Repositories(s)
            assert r.strategy_status.get("sweep_mss_fvg", "1.0").status == "DEV"
            row = r.strategy_status.get("sweep_mss_ob", "1.0")
            assert row.status == "OOS_PASSED" and row.max_class == "A_PLUS"
