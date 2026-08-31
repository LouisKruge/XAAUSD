"""Phase 6: macro, DXY, calendar, news. The theme is fail-safe degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from xauusd.data.providers.calendar_feed import (
    FallbackCalendarProvider,
    LayeredCalendarProvider,
    mask_future_actuals,
)
from xauusd.data.providers.news_feed import parse_feed
from xauusd.domain.enums import Bias, Direction, EventImpact, MacroBias, NewsRisk
from xauusd.intelligence.dxy import (
    InsufficientDxyData,
    correlation_with_gold,
    dxy_state,
    synthetic_dxy,
)
from xauusd.intelligence.economic_calendar import (
    CalendarEvent,
    CalendarFilter,
    RecurringEventSchedule,
    classify_event,
)
from xauusd.intelligence.macro import MacroEngine, MacroInputs
from xauusd.intelligence.news import NewsEngine, NewsItem, assess_by_rules, is_gold_relevant

UTC = UTC
NOW = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)


def _majors(n: int = 60, strengthening: bool = True) -> dict[str, np.ndarray]:
    d = 1 if strengthening else -1
    return {
        "EURUSD": np.linspace(1.10, 1.10 - 0.05 * d, n),
        "USDJPY": np.linspace(145, 145 + 7 * d, n),
        "GBPUSD": np.linspace(1.28, 1.28 - 0.04 * d, n),
        "USDCAD": np.linspace(1.34, 1.34 + 0.04 * d, n),
        "USDSEK": np.linspace(10.2, 10.2 + 0.5 * d, n),
        "USDCHF": np.linspace(0.86, 0.86 + 0.04 * d, n),
    }


class TestSyntheticDXY:
    def test_matches_the_real_index_at_realistic_levels(self) -> None:
        """Sanity check against a known point: these majors correspond to DXY ~104."""
        closes = {
            "EURUSD": np.array([1.0850]),
            "USDJPY": np.array([149.50]),
            "GBPUSD": np.array([1.2650]),
            "USDCAD": np.array([1.3600]),
            "USDSEK": np.array([10.450]),
            "USDCHF": np.array([0.8800]),
        }
        assert float(synthetic_dxy(closes)[0]) == pytest.approx(104.0, abs=1.5)

    def test_requires_all_six_pairs(self) -> None:
        closes = _majors()
        del closes["USDSEK"]
        with pytest.raises(InsufficientDxyData, match="USDSEK"):
            synthetic_dxy(closes)

    def test_rejects_misaligned_series(self) -> None:
        closes = _majors()
        closes["EURUSD"] = closes["EURUSD"][:-5]
        with pytest.raises(InsufficientDxyData, match="different lengths"):
            synthetic_dxy(closes)

    def test_direction(self) -> None:
        up = dxy_state(synthetic_dxy(_majors(strengthening=True)))
        down = dxy_state(synthetic_dxy(_majors(strengthening=False)))
        assert up.trend is Bias.BULLISH and up.gold_implication is Bias.BEARISH
        assert down.trend is Bias.BEARISH and down.gold_implication is Bias.BULLISH

    def test_correlation_breakdown_is_reported_not_hidden(self) -> None:
        """Gold and DXY rising together is information, not an error."""
        n = 80
        dxy = np.linspace(100, 105, n)
        gold = np.linspace(2000, 2100, n)  # both rising: positive correlation
        c = correlation_with_gold(dxy, gold, 60)
        assert np.isfinite(c)


class TestMacro:
    def _inputs(self, ry_from: float, ry_to: float, dxy_up: bool | None = None) -> MacroInputs:
        pts = [
            (NOW - timedelta(days=10 - i), ry_from + (ry_to - ry_from) * i / 9) for i in range(10)
        ]
        flat = [(NOW - timedelta(days=10 - i), 4.2) for i in range(10)]
        d = None
        if dxy_up is not None:
            d = dxy_state(synthetic_dxy(_majors(strengthening=dxy_up)))
        return MacroInputs(pts, flat, flat, flat, flat, NOW, d)

    def test_falling_real_yields_are_bullish_gold(self) -> None:
        state, _ = MacroEngine().classify(self._inputs(2.10, 1.80, dxy_up=False))
        assert state.bias in (MacroBias.BULLISH, MacroBias.STRONGLY_BULLISH)

    def test_rising_real_yields_are_bearish_gold(self) -> None:
        state, _ = MacroEngine().classify(self._inputs(1.80, 2.20, dxy_up=True))
        assert state.bias in (MacroBias.BEARISH, MacroBias.STRONGLY_BEARISH)

    def test_real_yields_carry_the_most_weight(self) -> None:
        e = MacroEngine()
        assert e.W_REAL_YIELD > e.W_DXY > e.W_NOMINAL

    def test_stale_data_is_unknown_not_neutral(self) -> None:
        """UNKNOWN blocks A+; NEUTRAL would not. The distinction matters."""
        old = MacroInputs([(NOW - timedelta(days=40), 2.0)], [], [], [], [], NOW)
        state, _ = MacroEngine().classify(old)
        assert state.bias is MacroBias.UNKNOWN
        assert not state.bias.is_known
        assert state.is_stale

    def test_explanation_is_produced(self) -> None:
        state, _ = MacroEngine().classify(self._inputs(2.1, 1.8, dxy_up=False))
        lines = MacroEngine.explain(state)
        assert any("real yield" in x for x in lines)


class TestCalendarClassification:
    @pytest.mark.parametrize(
        "name,currency,impact,min_relevance",
        [
            ("FOMC Rate Decision", "USD", EventImpact.CRITICAL, 10),
            ("Core CPI m/m", "USD", EventImpact.CRITICAL, 10),
            ("Non-Farm Payrolls", "USD", EventImpact.CRITICAL, 9),
            ("Initial Jobless Claims", "USD", EventImpact.MEDIUM, 5),
        ],
    )
    def test_gold_relevant_events(
        self, name: str, currency: str, impact: EventImpact, min_relevance: int
    ) -> None:
        i, r, _ = classify_event(name, currency)
        assert i is impact and r >= min_relevance

    def test_irrelevant_events_score_zero(self) -> None:
        for name in ("German Consumer Confidence", "Cattle on Feed", "Tourist Arrivals"):
            i, r, k = classify_event(name, "USD")
            assert i is EventImpact.LOW and r == 0 and k is None

    def test_non_usd_events_are_downgraded_for_gold(self) -> None:
        usd, _, _ = classify_event("FOMC Rate Decision", "USD")
        eur, rel, _ = classify_event("ECB Rate Decision", "EUR")
        assert usd is EventImpact.CRITICAL
        assert eur is EventImpact.HIGH and rel < 10


class TestBlackout:
    CPI = CalendarEvent(
        datetime(2026, 6, 10, 12, 30, tzinfo=UTC),
        "US CPI",
        "USD",
        EventImpact.CRITICAL,
        10,
        "US_CPI",
    )

    def test_pre_event_blackout(self) -> None:
        f = CalendarFilter()
        assert not f.evaluate(datetime(2026, 6, 10, 10, 30, tzinfo=UTC), [self.CPI]).blocks_entry
        st = f.evaluate(datetime(2026, 6, 10, 11, 45, tzinfo=UTC), [self.CPI])
        assert st.blocks_entry and st.phase == "PRE"

    def test_post_event_reentry_requires_normalisation_not_just_time(self) -> None:
        """Waiting 30 minutes then trading into a 3x spread is how bots get filled badly."""
        f = CalendarFilter()
        t = datetime(2026, 6, 10, 13, 10, tzinfo=UTC)  # timer expired
        wild = f.evaluate(t, [self.CPI], spread_ratio=3.0, atr_ratio=3.5)
        assert wild.blocks_entry and wild.phase == "STABILISING"
        assert "not normalised" in (wild.reason or "")

        calm = f.evaluate(t, [self.CPI], spread_ratio=1.1, atr_ratio=1.2)
        assert not calm.blocks_entry and calm.phase == "CLEAR"

    def test_low_relevance_events_do_not_blackout(self) -> None:
        minor = CalendarEvent(
            datetime(2026, 6, 10, 12, 30, tzinfo=UTC),
            "Tourist Arrivals",
            "NZD",
            EventImpact.LOW,
            0,
            None,
        )
        st = CalendarFilter().evaluate(datetime(2026, 6, 10, 12, 25, tzinfo=UTC), [minor])
        assert not st.blocks_entry

    def test_next_event_is_reported_for_the_dashboard(self) -> None:
        st = CalendarFilter().evaluate(datetime(2026, 6, 10, 8, 0, tzinfo=UTC), [self.CPI])
        assert st.next_event is not None
        assert st.minutes_to_next == pytest.approx(270.0)


class TestCalendarProviders:
    def test_fallback_never_fails(self) -> None:
        p = LayeredCalendarProvider([FallbackCalendarProvider()])
        evs = p.events(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC))
        assert evs and p.last_source == "curated_fallback"

    def test_fallback_covers_the_events_that_matter(self) -> None:
        evs = RecurringEventSchedule().events_between(
            datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC)
        )
        keys = {e.normalized_key for e in evs}
        assert "US_NFP" in keys and "US_CPI" in keys

    def test_future_actuals_are_masked(self) -> None:
        """A calendar row that already knows Friday's NFP leaks the future directly."""
        evs = [
            CalendarEvent(
                datetime(2026, 6, 5, 12, 30, tzinfo=UTC),
                "NFP",
                "USD",
                EventImpact.CRITICAL,
                9,
                "US_NFP",
                actual=250.0,
            ),
            CalendarEvent(
                datetime(2026, 6, 12, 12, 30, tzinfo=UTC),
                "CPI",
                "USD",
                EventImpact.CRITICAL,
                10,
                "US_CPI",
                actual=3.1,
            ),
        ]
        masked = mask_future_actuals(evs, datetime(2026, 6, 10, tzinfo=UTC))
        assert masked[0].actual == 250.0  # already released
        assert masked[1].actual is None  # not yet released


class TestNews:
    def _state(self, items: list[NewsItem], feed_age: float | None = 5.0, cal: bool = False):  # type: ignore[no-untyped-def]
        e = NewsEngine()
        return e.aggregate(
            [(i, e.assess(i, NOW)) for i in items],
            NOW,
            calendar_blackout=cal,
            feed_age_minutes=feed_age,
        )

    def test_rules_classify_the_big_categories(self) -> None:
        cases = [
            ("Israel launches air strikes on Iranian nuclear facilities", "WAR", Bias.BULLISH),
            (
                "Fed announces emergency rate cut at unscheduled meeting",
                "CENTRAL_BANK",
                Bias.BULLISH,
            ),
            ("Regional bank collapse triggers contagion fears", "BANKING", Bias.BULLISH),
            ("Ceasefire agreed, peace talks to begin", "WAR", Bias.BEARISH),
        ]
        for headline, category, direction in cases:
            a = assess_by_rules(NewsItem(NOW, headline, "test"), NOW)
            assert a.category == category
            assert a.direction is direction
            assert a.gold_relevance >= 6

    def test_irrelevant_news_is_filtered_out(self) -> None:
        assert not is_gold_relevant(NewsItem(NOW, "Local sports team wins final", "x"))

    def test_escalation_raises_risk(self) -> None:
        st = self._state(
            [
                NewsItem(
                    NOW - timedelta(minutes=10),
                    "Israel launches air strikes on Iranian nuclear facilities",
                    "reuters",
                ),
                NewsItem(NOW - timedelta(minutes=30), "Missile attack on Red Sea shipping", "ap"),
            ]
        )
        assert st.risk.level >= NewsRisk.HIGH.level
        assert st.directional_hint is Bias.BULLISH
        assert st.drivers

    def test_quiet_tape_is_low_risk(self) -> None:
        st = self._state([NewsItem(NOW - timedelta(hours=1), "Company X earnings beat", "x")])
        assert st.risk is NewsRisk.LOW

    def test_dead_feed_degrades_to_moderate_never_low(self) -> None:
        """Absence of news is not evidence of calm, and LOW would unlock A+."""
        st = self._state([], feed_age=None)
        assert st.risk is NewsRisk.MODERATE
        assert st.is_stale

    def test_stale_feed_floors_risk(self) -> None:
        st = self._state(
            [NewsItem(NOW - timedelta(hours=1), "Company X earnings", "x")], feed_age=120.0
        )
        assert st.risk is NewsRisk.MODERATE

    def test_calendar_blackout_raises_risk_to_high(self) -> None:
        st = self._state([NewsItem(NOW - timedelta(hours=1), "Company X earnings", "x")], cal=True)
        assert st.risk.level >= NewsRisk.HIGH.level and st.blackout

    def test_score_contribution_is_bounded(self) -> None:
        """News can never, alone, move a candidate across a classification threshold."""
        e = NewsEngine()
        st = self._state(
            [
                NewsItem(NOW - timedelta(minutes=5), "Nuclear strike reported", "reuters"),
                NewsItem(NOW - timedelta(minutes=6), "Bank collapse contagion", "reuters"),
                NewsItem(NOW - timedelta(minutes=7), "Emergency rate cut announced", "reuters"),
            ]
        )
        for direction in (Direction.LONG, Direction.SHORT):
            assert abs(e.score_contribution(st, direction)) <= e.cfg.max_news_score_contribution

    def test_stale_news_contributes_nothing(self) -> None:
        e = NewsEngine()
        assert e.score_contribution(self._state([], feed_age=None), Direction.LONG) == 0.0


class TestFeedParsing:
    def test_parses_rss(self) -> None:
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Fed signals rate cut</title><description>text</description>
        <pubDate>Wed, 10 Jun 2026 14:30:00 GMT</pubDate></item></channel></rss>"""
        items = parse_feed(rss, "test")
        assert len(items) == 1
        assert items[0].published_ts == datetime(2026, 6, 10, 14, 30, tzinfo=UTC)

    def test_malformed_feed_yields_nothing_rather_than_raising(self) -> None:
        assert parse_feed("<not xml", "test") == []


class TestTerminalCalendarRelay:
    """The MQL5 relay file is the PRIMARY calendar source — free, already installed,
    and on the broker's own clock."""

    def _write(self, tmp_path, generated_at: float, events: list) -> str:  # type: ignore[no-untyped-def]
        import json

        f = tmp_path / "cal.json"
        f.write_text(json.dumps({"generated_at": generated_at, "events": events}))
        return str(f)

    def _event(self, offset_s: float, name: str, currency: str = "USD", **kw):  # type: ignore[no-untyped-def]
        import time

        base = {
            "event_id": 1,
            "time": time.time() + offset_s,
            "name": name,
            "currency": currency,
            "importance": 3,
            "actual": None,
            "forecast": None,
            "previous": None,
        }
        base.update(kw)
        return base

    def test_reads_and_classifies_relayed_events(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import time

        from xauusd.data.providers.calendar_feed import build_default_chain

        path = self._write(
            tmp_path,
            time.time(),
            [
                self._event(3600, "Non-Farm Payrolls"),
                self._event(7200, "German Consumer Confidence", "EUR"),
            ],
        )
        chain = build_default_chain(terminal_file=path)
        evs = chain.events(datetime.now(UTC), datetime.now(UTC) + timedelta(hours=6))
        assert chain.last_source == "mt5_terminal_file"
        nfp = next(e for e in evs if "Payrolls" in e.name)
        assert nfp.impact is EventImpact.CRITICAL and nfp.gold_relevance >= 9

    def test_a_stale_relay_file_falls_through_rather_than_masking_events(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A stale file is worse than no file: it would silently hide new events."""
        import time

        from xauusd.data.providers.calendar_feed import build_default_chain

        path = self._write(tmp_path, time.time() - 86400, [])
        chain = build_default_chain(terminal_file=path)
        chain.events(datetime.now(UTC), datetime.now(UTC) + timedelta(days=10))
        assert chain.last_source == "curated_fallback"

    def test_a_missing_file_falls_through(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from xauusd.data.providers.calendar_feed import build_default_chain

        chain = build_default_chain(terminal_file=str(tmp_path / "nope.json"))
        chain.events(datetime.now(UTC), datetime.now(UTC) + timedelta(days=10))
        assert chain.last_source == "curated_fallback"

    def test_a_corrupt_file_falls_through(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from xauusd.data.providers.calendar_feed import build_default_chain

        f = tmp_path / "cal.json"
        f.write_text("{not json")
        chain = build_default_chain(terminal_file=str(f))
        chain.events(datetime.now(UTC), datetime.now(UTC) + timedelta(days=10))
        assert chain.last_source == "curated_fallback"

    def test_unreleased_actuals_stay_none(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The EA emits null rather than a sentinel; a sentinel read as a real value
        would be a direct route to a look-ahead bug."""
        import time

        from xauusd.data.providers.calendar_feed import build_default_chain

        path = self._write(tmp_path, time.time(), [self._event(3600, "US CPI", forecast=3.1)])
        chain = build_default_chain(terminal_file=path)
        evs = chain.events(datetime.now(UTC), datetime.now(UTC) + timedelta(hours=6))
        assert evs[0].actual is None
        assert evs[0].forecast == 3.1
