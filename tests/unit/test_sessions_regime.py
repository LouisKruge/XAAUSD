"""Sessions must survive DST; regimes must refuse to guess."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.fixtures.synthetic import ranging, trend
from xauusd.core.regime import RegimeEngine
from xauusd.core.sessions import LONDON, NEW_YORK, BrokerClock, SessionEngine
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Killzone, Regime, Session, Timeframe
from xauusd.domain.types import Bar

UTC = UTC


def U(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)  # type: ignore[arg-type]


class TestSessionDST:
    def test_london_open_shifts_with_dst(self) -> None:
        """08:00 London is 08:00 UTC in winter and 07:00 UTC in summer."""
        e = SessionEngine()
        assert e.session_for(U(2026, 1, 15, 8, 30)) in (Session.LONDON, Session.OVERLAP)
        assert e.session_for(U(2026, 1, 15, 7, 30)) not in (Session.LONDON, Session.OVERLAP)
        assert e.session_for(U(2026, 7, 15, 7, 30)) in (Session.LONDON, Session.OVERLAP)

    def test_dst_disagreement_week_is_handled(self) -> None:
        """Between the US and UK DST switches the overlap shifts by an hour."""
        e = SessionEngine()
        march = U(2026, 3, 10, 13, 0)  # US on DST, UK not yet
        april = U(2026, 4, 7, 13, 0)  # both on DST
        assert march.astimezone(LONDON).hour == 13
        assert march.astimezone(NEW_YORK).hour == 9
        assert april.astimezone(LONDON).hour == 14
        assert april.astimezone(NEW_YORK).hour == 9
        assert e.session_for(march) is Session.OVERLAP
        assert e.session_for(april) is Session.OVERLAP

    def test_killzones(self) -> None:
        e = SessionEngine()
        assert e.killzone_for(U(2026, 1, 15, 8, 0)) is Killzone.LONDON_KZ
        assert e.killzone_for(U(2026, 1, 15, 14, 0)) is Killzone.NY_AM_KZ
        assert e.killzone_for(U(2026, 1, 15, 19, 0)) is Killzone.NY_PM_KZ

    def test_weekend_is_closed(self) -> None:
        e = SessionEngine()
        assert not e.is_market_open(U(2026, 1, 10, 12))  # Saturday
        assert not e.is_market_open(U(2026, 1, 11, 12))  # Sunday morning
        assert e.is_market_open(U(2026, 1, 11, 23))  # Sunday after the open
        assert not e.is_market_open(U(2026, 1, 9, 22))  # Friday after the close

    def test_thin_liquidity_windows_are_blocked(self) -> None:
        e = SessionEngine()
        ok, why = e.is_tradable_window(U(2026, 1, 9, 20, 30))  # Friday, near close
        assert not ok and "close" in why
        ok, why = e.is_tradable_window(U(2026, 1, 7, 14, 0))  # Wednesday overlap
        assert ok

    def test_holidays_close_the_market(self) -> None:
        e = SessionEngine()
        e.add_holidays([U(2026, 1, 7).date()])
        assert not e.is_market_open(U(2026, 1, 7, 14))


class TestBrokerClock:
    def test_offset_is_measured_not_configured(self) -> None:
        c = BrokerClock()
        c.observe(U(2026, 1, 5, 14, 0), U(2026, 1, 5, 12, 0))
        assert c.offset_hours == 2.0

    def test_dst_jump_is_detected(self) -> None:
        c = BrokerClock()
        c.observe(U(2026, 1, 5, 14, 0), U(2026, 1, 5, 12, 0))
        jump = c.observe(U(2026, 3, 10, 15, 0), U(2026, 3, 10, 12, 0))
        assert jump == 3600

    def test_quote_latency_is_not_mistaken_for_a_jump(self) -> None:
        c = BrokerClock()
        c.observe(U(2026, 1, 5, 14, 0, 0), U(2026, 1, 5, 12, 0, 0))
        assert c.observe(U(2026, 1, 5, 14, 0, 3), U(2026, 1, 5, 12, 0, 0)) is None


class TestRegime:
    def test_trends_and_ranges_are_distinguished(self) -> None:
        e = RegimeEngine()
        assert e.classify(trend(300, drift=1.2, noise=0.6)).regime is Regime.STRONG_BULL
        assert e.classify(trend(300, drift=-1.2, noise=0.6)).regime is Regime.STRONG_BEAR
        assert e.classify(ranging(300)).regime is Regime.RANGE

    def test_insufficient_history_is_abnormal_not_a_guess(self) -> None:
        r = RegimeEngine().classify(trend(30))
        assert r.regime is Regime.ABNORMAL
        assert not r.is_tradable
        assert "insufficient history" in r.reasons[0]

    def test_volatility_shock_overrides_a_trend_label(self) -> None:
        """A market this far outside its own behaviour is not one we claim to read."""
        s = trend(300, drift=1.0, noise=0.5)
        bars = s.to_bars()
        last = bars[-1]
        bars[-1] = Bar(last.ts, last.open, last.open + 150, last.open - 150, last.close)
        r = RegimeEngine().classify(BarSeries.from_bars(Timeframe.H1, bars))
        assert r.regime is Regime.ABNORMAL
        assert not r.is_tradable

    def test_abnormal_spread_is_untradable(self) -> None:
        r = RegimeEngine().classify(trend(300), spread_points=200.0, spread_median=25.0)
        assert r.regime is Regime.ABNORMAL

    def test_abnormal_is_never_tradable(self) -> None:
        assert not Regime.ABNORMAL.is_tradable
        assert all(r.is_tradable for r in Regime if r is not Regime.ABNORMAL)
