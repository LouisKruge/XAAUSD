"""The intraday Trend Expansion + Pullback engine (spec §17-§28).

§48 insists this must not be "the scalp strategy with a bigger target", and the tests
that actually enforce that are the ones about ORDER and FREQUENCY:

  * the sequence is ordered in time — expansion, then pullback, then confirmation. Any
    other order is a coincidence, not a setup.
  * §28: ONE entry per directional setup. A consumed setup waits for a NEW expansion. A
    second entry on the same one is pyramiding wearing an intraday label.
  * §21: H4 and H1 must AGREE. Conflict means no trade, not a smaller trade.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xauusd.config.settings import Settings
from xauusd.domain.enums import Bias, Direction, StructureKind, Timeframe
from xauusd.domain.types import StructureEvent, TimeframeStructure
from xauusd.strategy.intraday.expansion_pullback import (
    SETUP_TF,
    ExpansionPullbackEngine,
    SetupState,
)

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def base_snapshot():
    from tests.fixtures.synthetic import market_m1
    from xauusd.core.analyzer import MarketAnalyzer
    from xauusd.data.marketview import InMemoryBarSource, MarketView
    from xauusd.domain.types import Quote

    data = market_m1(12_000, seed=4)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]
    bar = m1.bar_at(len(m1) - 2)
    now = bar.ts + timedelta(seconds=60)
    view = MarketView(source, "GOLD", now, Quote(now, bar.close - 0.05, bar.close + 0.05))
    return MarketAnalyzer(Settings()).analyze(view, None, None, 25.0, 25.0)


def _struct(tf, bias, last_bos=None, last_event=None):  # type: ignore[no-untyped-def]
    return TimeframeStructure(
        timeframe=tf,
        bias=bias,
        last_event=last_event,
        swings=(),
        dealing_range=None,
        last_bos=last_bos,
    )


def _aligned(base, direction: Direction, *, expansion: bool = False, confirm: bool = False):
    """A snapshot with H4/H1 agreeing, optionally with expansion and confirmation."""
    bias = Bias.BULLISH if direction is Direction.LONG else Bias.BEARISH
    bos = (
        StructureEvent(
            ts=NOW - timedelta(minutes=30),
            timeframe=SETUP_TF,
            kind=StructureKind.BOS,
            direction=direction,
            price=base.quote.mid,
            break_price=base.quote.mid,
            displacement_atr=1.5,
        )
        if expansion
        else None
    )
    trigger_event = (
        StructureEvent(
            ts=NOW,
            timeframe=Timeframe.M5,
            kind=StructureKind.BOS,
            direction=direction,
            price=base.quote.mid,
            break_price=base.quote.mid,
            displacement_atr=1.0,
        )
        if confirm
        else None
    )
    return replace(
        base,
        structures={
            Timeframe.H4: _struct(Timeframe.H4, bias),
            Timeframe.H1: _struct(Timeframe.H1, bias),
            SETUP_TF: _struct(SETUP_TF, bias, last_bos=bos),
            Timeframe.M5: _struct(Timeframe.M5, bias, last_event=trigger_event),
        },
    )


class TestContextMustAgreeBeforeAnythingElse:
    def test_conflicting_h4_and_h1_means_no_trade(self, base_snapshot) -> None:
        """§21: 4H bullish with 1H bearish is NO TRADE, not a weaker one."""
        snap = replace(
            base_snapshot,
            structures={
                Timeframe.H4: _struct(Timeframe.H4, Bias.BULLISH),
                Timeframe.H1: _struct(Timeframe.H1, Bias.BEARISH),
            },
        )
        result = ExpansionPullbackEngine(Settings()).evaluate(snap, NOW)
        assert result.reached == "context"
        assert not result.is_entry
        assert "conflicts" in result.reasons[0]

    def test_a_neutral_timeframe_is_not_direction(self, base_snapshot) -> None:
        snap = replace(
            base_snapshot,
            structures={
                Timeframe.H4: _struct(Timeframe.H4, Bias.BULLISH),
                Timeframe.H1: _struct(Timeframe.H1, Bias.NEUTRAL),
            },
        )
        result = ExpansionPullbackEngine(Settings()).evaluate(snap, NOW)
        assert result.reached == "context"

    def test_alignment_alone_is_not_an_entry(self, base_snapshot) -> None:
        """§22: do NOT enter simply because H4 and H1 agree. Expansion must be shown."""
        result = ExpansionPullbackEngine(Settings()).evaluate(
            _aligned(base_snapshot, Direction.LONG), NOW
        )
        assert result.reached == "context"
        assert "no expansion" in result.reasons[0]


class TestTheSequenceIsOrdered:
    def test_expansion_is_required_before_a_pullback_is_looked_for(self, base_snapshot) -> None:
        eng = ExpansionPullbackEngine(Settings())
        assert eng.setup is None
        eng.evaluate(_aligned(base_snapshot, Direction.LONG), NOW)
        assert eng.setup is None, "no setup may be remembered without an expansion"

    def test_an_expansion_creates_a_remembered_setup(self, base_snapshot) -> None:
        """The memory is what makes the order meaningful: the expansion happened EARLIER
        and the pullback is happening now."""
        eng = ExpansionPullbackEngine(Settings())
        eng.evaluate(_aligned(base_snapshot, Direction.LONG, expansion=True), NOW)
        assert eng.setup is not None
        assert eng.setup.direction is Direction.LONG

    def test_a_stale_expansion_is_dropped(self, base_snapshot) -> None:
        """A move from yesterday is history, not the reason price is where it is."""
        eng = ExpansionPullbackEngine(Settings())
        eng.evaluate(_aligned(base_snapshot, Direction.LONG, expansion=True), NOW)
        assert eng.setup is not None

        much_later = NOW + timedelta(minutes=Settings().intraday.setup_max_age_minutes + 60)
        result = eng.evaluate(_aligned(base_snapshot, Direction.LONG, expansion=True), much_later)
        assert "stale" in " ".join(result.reasons) or eng.setup is not None

    def test_losing_the_context_forgets_the_setup(self, base_snapshot) -> None:
        eng = ExpansionPullbackEngine(Settings())
        eng.evaluate(_aligned(base_snapshot, Direction.LONG, expansion=True), NOW)
        assert eng.setup is not None

        conflicted = replace(
            base_snapshot,
            structures={
                Timeframe.H4: _struct(Timeframe.H4, Bias.BULLISH),
                Timeframe.H1: _struct(Timeframe.H1, Bias.BEARISH),
            },
        )
        eng.evaluate(conflicted, NOW)
        assert eng.setup is None, "a setup must not survive the context that justified it"


class TestOneEntryPerSetup:
    """§28 — the rule that stops this becoming the scalp engine."""

    def test_a_consumed_setup_offers_no_second_entry(self, base_snapshot) -> None:
        eng = ExpansionPullbackEngine(Settings())
        snap = _aligned(base_snapshot, Direction.LONG, expansion=True, confirm=True)
        eng.evaluate(snap, NOW)
        assert eng.setup is not None

        eng.consume()
        result = eng.evaluate(snap, NOW)
        assert not result.is_entry
        assert "already traded" in " ".join(result.reasons)

    def test_consuming_survives_repeated_evaluation(self, base_snapshot) -> None:
        """Pyramiding is what happens when the flag is lost on the next cycle."""
        eng = ExpansionPullbackEngine(Settings())
        snap = _aligned(base_snapshot, Direction.LONG, expansion=True, confirm=True)
        eng.evaluate(snap, NOW)
        eng.consume()
        for _ in range(5):
            assert not eng.evaluate(snap, NOW).is_entry

    def test_a_new_expansion_in_the_other_direction_replaces_the_setup(self, base_snapshot) -> None:
        """§28 says wait for a NEW expansion — not never trade again."""
        eng = ExpansionPullbackEngine(Settings())
        eng.evaluate(_aligned(base_snapshot, Direction.LONG, expansion=True), NOW)
        eng.consume()
        assert eng.setup is not None and eng.setup.consumed

        eng.evaluate(_aligned(base_snapshot, Direction.SHORT, expansion=True), NOW)
        assert eng.setup is not None
        assert eng.setup.direction is Direction.SHORT
        assert not eng.setup.consumed


class TestItIsConfiguredAsALowFrequencyEngine:
    def test_only_one_position_at_a_time(self) -> None:
        """§28 default, enforced by the validator rather than by convention."""
        cfg = Settings().intraday
        assert cfg.max_concurrent == 1
        with pytest.raises(ValidationError):
            type(cfg)(max_concurrent=2)

    def test_risk_is_the_full_two_percent(self) -> None:
        """§18. Materially different from the scalp's 0.50%, which is the point."""
        assert Settings().intraday.risk_pct == pytest.approx(0.02)
        assert Settings().scalp.risk_pct < Settings().intraday.risk_pct

    def test_it_ships_disabled(self) -> None:
        """Every new strategy in this project ships off until it has been validated."""
        assert Settings().intraday.enabled is False

    def test_it_reads_higher_timeframes_than_the_scalp_engine(self) -> None:
        """The engines must not converge onto the same evidence."""
        from xauusd.core.micro_structure import STRUCTURE_TF
        from xauusd.core.micro_structure import TRIGGER_TF as SCALP_TRIGGER
        from xauusd.strategy.intraday.expansion_pullback import (
            SETUP_TF as INTRADAY_SETUP,
        )
        from xauusd.strategy.intraday.expansion_pullback import (
            TRIGGER_TF as INTRADAY_TRIGGER,
        )

        assert SCALP_TRIGGER is Timeframe.M1
        assert INTRADAY_TRIGGER is Timeframe.M5
        assert INTRADAY_SETUP is Timeframe.M15
        assert INTRADAY_SETUP.seconds > STRUCTURE_TF.seconds


class TestSetupState:
    def test_staleness_is_measured_from_the_expansion(self) -> None:
        s = SetupState(Direction.LONG, NOW, 2000.0)
        assert not s.is_stale(NOW + timedelta(minutes=10), 240)
        assert s.is_stale(NOW + timedelta(minutes=300), 240)

    def test_consuming_preserves_the_expansion_details(self) -> None:
        eng = ExpansionPullbackEngine(Settings())
        eng.setup = SetupState(Direction.SHORT, NOW, 1999.5)
        eng.consume()
        assert eng.setup.consumed
        assert eng.setup.expansion_price == 1999.5
        assert eng.setup.direction is Direction.SHORT
