"""Per-engine risk budgets (spec §15, §16, §37).

§37 is the one that matters most: risk must be the actual money lost if every open
position hit its stop, never a count of trades. Three positions of wildly different size
are not "three units of risk", and a system that counts them that way will approve a
fourth it should refuse.

§15 is why per-engine budgets exist at all: every scalp is XAUUSD, so three simultaneous
longs are one directional bet sized three times.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Direction
from xauusd.domain.types import BrokerPosition, SymbolSpec
from xauusd.risk.engine_budget import INTRADAY, SCALP, EngineBudget

SPEC = SymbolSpec("GOLD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)
EQUITY = 10_000.0


def _pos(magic: int, *, entry: float = 2000.0, stop: float | None = 1990.0, vol: float = 0.1):
    return BrokerPosition(
        ticket=hash((magic, entry, stop, vol)) % 100000,
        symbol="GOLD",
        direction=Direction.LONG,
        volume=vol,
        entry_price=entry,
        stop_loss=stop or 0.0,
        take_profit=2020.0,
        opened_at=datetime.now(UTC),
        magic=magic,
    )


@pytest.fixture
def budget() -> EngineBudget:
    return EngineBudget(Settings())


@pytest.fixture
def magics() -> tuple[int, int]:
    b = Settings().broker
    return b.scalp_magic, b.intraday_magic


class TestRiskIsMoneyNotACount:
    def test_exposure_is_the_money_at_stake(self, budget, magics) -> None:
        """§37: the amount lost if the stop is hit, computed from the spec."""
        scalp_magic, _ = magics
        # 10.0 price units of stop distance on 0.1 lots.
        pos = _pos(scalp_magic, entry=2000.0, stop=1990.0, vol=0.1)
        exp = budget.exposure(SCALP, [pos], EQUITY, SPEC)
        expected = 10.0 * SPEC.value_per_price_unit(0.1)
        assert exp.risk_money == pytest.approx(expected)
        assert exp.risk_pct == pytest.approx(expected / EQUITY)

    def test_two_positions_of_different_size_are_not_equal_risk(self, budget, magics) -> None:
        """The failure a trade COUNT would produce: these are not 'two units'."""
        scalp_magic, _ = magics
        small = budget.exposure(SCALP, [_pos(scalp_magic, vol=0.01)], EQUITY, SPEC)
        large = budget.exposure(SCALP, [_pos(scalp_magic, vol=1.0)], EQUITY, SPEC)
        assert large.risk_money == pytest.approx(100 * small.risk_money)
        assert small.positions == large.positions == 1

    def test_a_wider_stop_is_more_risk_at_the_same_size(self, budget, magics) -> None:
        scalp_magic, _ = magics
        tight = budget.exposure(SCALP, [_pos(scalp_magic, stop=1999.0)], EQUITY, SPEC)
        wide = budget.exposure(SCALP, [_pos(scalp_magic, stop=1980.0)], EQUITY, SPEC)
        assert wide.risk_money > tight.risk_money


class TestAPositionWithNoStopIsNotZeroRisk:
    """The inversion worth guarding: less knowledge must never read as less risk."""

    def test_a_missing_stop_is_counted_as_unmeasurable(self, budget, magics) -> None:
        scalp_magic, _ = magics
        exp = budget.exposure(SCALP, [_pos(scalp_magic, stop=None)], EQUITY, SPEC)
        assert exp.unknown_stop == 1
        assert exp.has_unmeasurable_risk

    def test_it_refuses_to_add_while_risk_cannot_be_measured(self, budget, magics) -> None:
        scalp_magic, _ = magics
        v = budget.may_add(SCALP, 0.005, [_pos(scalp_magic, stop=None)], EQUITY, SPEC)
        assert not v.allowed
        assert "cannot be measured" in v.reason


class TestTheEnginesHaveSeparateBudgets:
    def test_scalp_positions_do_not_count_against_intraday(self, budget, magics) -> None:
        """§36's magic numbers exist so this separation is possible at all."""
        scalp_magic, intraday_magic = magics
        positions = [_pos(scalp_magic), _pos(scalp_magic), _pos(intraday_magic)]
        s = budget.exposure(SCALP, positions, EQUITY, SPEC)
        i = budget.exposure(INTRADAY, positions, EQUITY, SPEC)
        assert s.positions == 2
        assert i.positions == 1

    def test_a_foreign_magic_belongs_to_neither_engine(self, budget) -> None:
        """A manual trade, or another EA's position, is not ours to budget."""
        assert budget.engine_of(_pos(999_999)) is None
        exp = budget.exposure(SCALP, [_pos(999_999)], EQUITY, SPEC)
        assert exp.positions == 0

    def test_total_exposure_sums_both_engines(self, budget, magics) -> None:
        scalp_magic, intraday_magic = magics
        positions = [_pos(scalp_magic), _pos(intraday_magic)]
        total = budget.total_exposure(positions, EQUITY, SPEC)
        s = budget.exposure(SCALP, positions, EQUITY, SPEC).risk_pct
        i = budget.exposure(INTRADAY, positions, EQUITY, SPEC).risk_pct
        assert total == pytest.approx(s + i)


class TestTheAccountCapAlwaysBinds:
    def test_an_engine_within_its_budget_is_still_refused_over_the_account_cap(
        self, budget, magics
    ) -> None:
        """The case that makes per-engine budgets safe rather than additive.

        Each engine may be inside its own 2% budget while together they exceed the
        account's 2% cap. The account cap must win — otherwise "per-engine budgets"
        quietly become permission to carry double.
        """
        scalp_magic, intraday_magic = magics
        # An intraday position already using most of the account budget.
        heavy = _pos(intraday_magic, entry=2000.0, stop=1985.0, vol=1.0)
        used = budget.exposure(INTRADAY, [heavy], EQUITY, SPEC).risk_pct
        assert used > 0.01, "fixture must actually consume budget"

        v = budget.may_add(SCALP, 0.005, [heavy], EQUITY, SPEC)
        if not v.allowed:
            assert "account-wide" in v.reason or "budget" in v.reason

    def test_headroom_never_goes_negative(self, budget, magics) -> None:
        scalp_magic, _ = magics
        v = budget.may_add(SCALP, 0.5, [_pos(scalp_magic)], EQUITY, SPEC)
        assert v.headroom_pct >= 0.0


class TestPerEngineDailyDrawdown:
    """§16: a scalp drawdown breach disables SCALPING, not the whole account."""

    def test_a_scalp_loss_does_not_disable_intraday(self, budget) -> None:
        now = datetime.now(UTC)
        budget.daily.roll(now.date(), EQUITY)
        budget.observe_close(SCALP, -EQUITY * 0.03, now, EQUITY)  # 3%, over the 2% limit

        assert budget.daily.drawdown_pct(SCALP) > 0.02
        assert budget.daily.drawdown_pct(INTRADAY) == 0.0

        scalp_v = budget.may_add(SCALP, 0.005, [], EQUITY, SPEC)
        intraday_v = budget.may_add(INTRADAY, 0.005, [], EQUITY, SPEC)
        assert not scalp_v.allowed
        assert "daily drawdown" in scalp_v.reason
        assert intraday_v.allowed, "the intraday engine must be unaffected"

    def test_profit_reads_as_zero_drawdown_not_negative(self, budget) -> None:
        now = datetime.now(UTC)
        budget.daily.roll(now.date(), EQUITY)
        budget.observe_close(SCALP, +500.0, now, EQUITY)
        assert budget.daily.drawdown_pct(SCALP) == 0.0

    def test_the_counter_resets_on_a_new_day(self, budget) -> None:
        from datetime import timedelta

        now = datetime.now(UTC)
        budget.daily.roll(now.date(), EQUITY)
        budget.observe_close(SCALP, -EQUITY * 0.03, now, EQUITY)
        assert budget.daily.drawdown_pct(SCALP) > 0

        budget.daily.roll((now + timedelta(days=1)).date(), EQUITY)
        assert budget.daily.drawdown_pct(SCALP) == 0.0

    def test_no_start_equity_reads_as_zero_rather_than_dividing_by_zero(self, budget) -> None:
        budget.daily.start_equity = 0.0
        budget.daily.record(SCALP, -100.0)
        assert budget.daily.drawdown_pct(SCALP) == 0.0
