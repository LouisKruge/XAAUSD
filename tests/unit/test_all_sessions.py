"""Round-the-clock trading, and what still stops it.

The session whitelist was removed from config because the clock was only ever a proxy
for the spread. These tests pin the two halves of that claim: the calendar no longer
refuses an Asian-session bar, and the things that genuinely must still refuse one —
a closed market, a weekend edge, an unaffordable spread — are untouched by the change.
"""

from __future__ import annotations

from datetime import UTC, datetime

from xauusd.config.settings import load_settings
from xauusd.core.sessions import SessionEngine
from xauusd.domain.enums import Session


def engine() -> SessionEngine:
    return SessionEngine(load_settings().session)


class TestEveryLiveSessionIsPermitted:
    def test_all_five_sessions_are_configured(self) -> None:
        allowed = set(load_settings().session.allowed_sessions)
        assert allowed == {
            Session.ASIA,
            Session.LONDON,
            Session.NEW_YORK,
            Session.OVERLAP,
            Session.OFF,
        }

    def test_an_asian_session_bar_is_now_tradable(self) -> None:
        """The exact rejection seen live: 04:40 UTC on a Thursday, session ASIA."""
        e = engine()
        ts = datetime(2026, 9, 3, 4, 40, tzinfo=UTC)  # Thursday, Asian session
        assert e.session_for(ts) is Session.ASIA
        ok, why = e.is_tradable_window(ts)
        assert ok, f"Asian session must now be tradable, got: {why}"

    def test_a_london_bar_is_still_tradable(self) -> None:
        e = engine()
        ok, _ = e.is_tradable_window(datetime(2026, 9, 3, 9, 0, tzinfo=UTC))
        assert ok


class TestTheRealGuardsAreUntouched:
    """Opening the calendar must not open the things the calendar was standing in for."""

    def test_the_weekend_is_still_refused(self) -> None:
        e = engine()
        ok, why = e.is_tradable_window(datetime(2026, 9, 5, 12, 0, tzinfo=UTC))  # Saturday
        assert not ok
        assert why

    def test_the_market_open_check_is_independent_of_the_session_list(self) -> None:
        """is_tradable_window consults is_market_open BEFORE the session list, so no
        entry in allowed_sessions can make a closed market tradable."""
        e = engine()
        for ts in (
            datetime(2026, 9, 5, 3, 0, tzinfo=UTC),  # Saturday
            datetime(2026, 9, 6, 3, 0, tzinfo=UTC),  # Sunday morning
        ):
            assert not e.is_market_open(ts)
            assert not e.is_tradable_window(ts)[0]


class TestTheSpreadIsNowTheFilter:
    """With the calendar open, the spread gate carries the load it was proxying for.
    Broker rollover runs 200+ points, where a round trip costs more than a $2 stop
    risks — so it must still be refused, on economics rather than on the clock."""

    def test_a_rollover_spread_exceeds_the_ceiling(self) -> None:
        ceiling = load_settings().execution.max_spread_points
        assert ceiling < 200.0, "a 200-point rollover spread must exceed the cap"

    def test_a_normal_asian_spread_is_within_the_ceiling(self) -> None:
        """~45 points: expensive but affordable. The engine may now evaluate it."""
        assert load_settings().execution.max_spread_points >= 45.0
