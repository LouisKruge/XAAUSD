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


class TestTheSessionGateNamesTheRealReason:
    """A gate trace must never contradict itself.

    From a live dashboard, the whole of what the operator was told:

        X  session   observed "LONDON" · required "['ASIA','LONDON','NEW_YORK','OVERLAP','OFF']"

    LONDON is in that list. The refusal was real — it was a Saturday and the market was
    closed — but `is_tradable_window` refuses for FIVE reasons and the gate reported only
    the session name against the allowed set, so four of the five rendered as nonsense.
    The reader is sent to audit session configuration for a fault that is the weekend.
    """

    def _ctx(self, ts):  # type: ignore[no-untyped-def]
        from dataclasses import replace

        from tests.integration.test_trade_path import perfect_snapshot

        snap = perfect_snapshot()
        return replace(snap, ts=ts)

    def test_a_weekend_refusal_says_market_closed(self) -> None:
        from datetime import UTC, datetime

        from xauusd.config.settings import Settings
        from xauusd.strategy.gates import GateContext, g_session

        # 2026-09-06 is a Saturday — the timestamp from the operator's dashboard.
        snap = self._ctx(datetime(2026, 9, 6, 6, 30, tzinfo=UTC))
        result = g_session(GateContext(settings=Settings(), snapshot=snap))

        assert not result.passed
        assert "closed" in str(result.observed).lower(), (
            f"the trace must say WHY, not just the session name: {result.observed!r}"
        )

    def test_the_observed_value_never_contradicts_the_requirement(self) -> None:
        """The precise defect: a failing gate whose observed value is in its own
        allowed list, with nothing to explain the difference."""
        from datetime import UTC, datetime

        from xauusd.config.settings import Settings
        from xauusd.strategy.gates import GateContext, g_session

        settings = Settings()
        allowed = {str(s) for s in settings.session.allowed_sessions}
        for day in range(1, 8):  # a whole week, weekdays and weekend alike
            snap = self._ctx(datetime(2026, 9, day, 10, 0, tzinfo=UTC))
            result = g_session(GateContext(settings=settings, snapshot=snap))
            if result.passed:
                continue
            observed = str(result.observed)
            assert observed not in allowed, (
                f"gate failed with observed {observed!r}, which its own threshold lists "
                f"as acceptable, and offered no other reason"
            )

    def test_a_passing_gate_still_reports_the_plain_session(self) -> None:
        """The reason is added on failure only; a passing trace stays clean."""
        from datetime import UTC, datetime

        from xauusd.config.settings import Settings
        from xauusd.strategy.gates import GateContext, g_session

        snap = self._ctx(datetime(2026, 9, 2, 9, 0, tzinfo=UTC))  # a Wednesday
        result = g_session(GateContext(settings=Settings(), snapshot=snap))
        if result.passed:
            assert "—" not in str(result.observed)
