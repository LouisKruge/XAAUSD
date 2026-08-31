"""Domain value objects: the invariants everything else depends on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from xauusd.domain.enums import (
    Bias,
    Direction,
    NewsRisk,
    Timeframe,
    ValidationStatus,
)
from xauusd.domain.types import (
    Bar,
    DealingRange,
    Quote,
    SymbolSpec,
    TargetLevel,
    TradePlan,
)

UTC = UTC
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def spec(**kw: object) -> SymbolSpec:
    base = dict(
        symbol="XAUUSD",
        digits=2,
        point=0.01,
        contract_size=100.0,
        tick_size=0.01,
        tick_value=1.0,
        tick_value_profit=1.0,
        tick_value_loss=1.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        stops_level=10,
        freeze_level=5,
    )
    base.update(kw)
    return SymbolSpec(**base)  # type: ignore[arg-type]


class TestTimeframe:
    def test_ordering(self) -> None:
        assert Timeframe.D1.rank > Timeframe.H4.rank > Timeframe.M15.rank

    def test_seconds(self) -> None:
        assert Timeframe.M5.seconds == 300
        assert Timeframe.H4.seconds == 14400


class TestBias:
    def test_conflict_detection(self) -> None:
        assert Bias.BULLISH.conflicts_with(Direction.SHORT)
        assert not Bias.NEUTRAL.conflicts_with(Direction.SHORT)
        assert Bias.BEARISH.agrees_with(Direction.SHORT)


class TestNewsRisk:
    def test_comparison(self) -> None:
        assert NewsRisk.LOW <= NewsRisk.HIGH
        assert not (NewsRisk.EXTREME <= NewsRisk.MODERATE)


class TestValidationStatus:
    def test_only_validated_strategies_are_live_eligible(self) -> None:
        assert not ValidationStatus.DEV.live_eligible
        assert not ValidationStatus.IN_SAMPLE_PASSED.live_eligible
        assert not ValidationStatus.FAILED.live_eligible
        assert ValidationStatus.OOS_PASSED.live_eligible


class TestBar:
    def test_geometry(self) -> None:
        b = Bar(T0, open=2000, high=2010, low=1990, close=2008)
        assert b.range == 20
        assert b.body == 8
        assert b.body_ratio == 0.4
        assert b.is_bullish
        assert b.upper_wick == 2
        assert b.lower_wick == 10

    def test_zero_range_bar_does_not_divide_by_zero(self) -> None:
        b = Bar(T0, 2000, 2000, 2000, 2000)
        assert b.body_ratio == 0.0


class TestQuote:
    def test_direction_aware_pricing(self) -> None:
        q = Quote(T0, bid=1999.50, ask=2000.00)
        assert q.price_for(Direction.LONG) == 2000.00
        assert q.price_for(Direction.SHORT) == 1999.50
        assert q.exit_price_for(Direction.LONG) == 1999.50
        assert q.spread == pytest.approx(0.5)
        assert q.spread_points(0.01) == pytest.approx(50.0)


class TestSymbolSpec:
    def test_rejects_impossible_specs(self) -> None:
        with pytest.raises(ValueError):
            spec(tick_size=0)
        with pytest.raises(ValueError):
            spec(volume_step=0)
        with pytest.raises(ValueError):
            spec(volume_min=0)
        with pytest.raises(ValueError):
            spec(tick_value_loss=0)

    def test_volume_always_floors(self) -> None:
        s = spec()
        assert s.normalize_volume(0.1749) == 0.17
        assert s.normalize_volume(0.179999) == 0.17
        assert s.normalize_volume(0.005) == 0.0

    @settings(max_examples=200, deadline=None)
    @given(
        vol=st.floats(0.0, 100.0, allow_nan=False),
        step=st.sampled_from([0.01, 0.1, 1.0, 0.05]),
    )
    def test_normalize_volume_never_rounds_up(self, vol: float, step: float) -> None:
        """Rounding may only ever REDUCE risk, never increase it."""
        s = spec(volume_step=step, volume_min=step, volume_max=1000.0)
        out = s.normalize_volume(vol)
        assert out <= vol + 1e-9
        if out > 0:
            assert abs(round(out / step) - out / step) < 1e-6

    def test_spec_hash_changes_with_contract_terms(self) -> None:
        assert spec().spec_hash() != spec(contract_size=10.0).spec_hash()
        assert spec().spec_hash() == spec().spec_hash()


class TestDealingRange:
    def test_premium_discount(self) -> None:
        dr = DealingRange(high=2100, low=2000, high_ts=T0, low_ts=T0, timeframe=Timeframe.H4)
        assert dr.equilibrium == 2050
        assert dr.is_discount(2010)
        assert dr.is_premium(2090)
        assert dr.zone_label(2010) == "DEEP_DISCOUNT"
        assert dr.zone_label(2060) == "PREMIUM"
        assert dr.position_of(2050) == 0.5

    def test_degenerate_range_is_neutral(self) -> None:
        dr = DealingRange(2000, 2000, T0, T0, Timeframe.H4)
        assert dr.position_of(2000) == 0.5


class TestTradePlan:
    def test_rejects_stop_on_wrong_side(self) -> None:
        with pytest.raises(ValueError):
            TradePlan(
                "s",
                "1",
                Direction.LONG,
                2000,
                2010,
                (TargetLevel(2020, 2, "x"),),
                T0,
                Timeframe.M15,
                "inv",
            )
        with pytest.raises(ValueError):
            TradePlan(
                "s",
                "1",
                Direction.SHORT,
                2000,
                1990,
                (TargetLevel(1980, 2, "x"),),
                T0,
                Timeframe.M15,
                "inv",
            )

    def test_requires_a_target(self) -> None:
        with pytest.raises(ValueError):
            TradePlan("s", "1", Direction.LONG, 2000, 1990, (), T0, Timeframe.M15, "inv")

    def test_reprice_recomputes_every_rr(self) -> None:
        """The whole point: a plan re-priced at execution must not carry a stale RR."""
        p = TradePlan(
            "s",
            "1",
            Direction.LONG,
            2000,
            1990,
            (TargetLevel(2020, 2.0, "tp1"), TargetLevel(2030, 3.0, "tp2")),
            T0,
            Timeframe.M15,
            "inv",
        )
        assert p.rr == 3.0
        moved = p.with_entry(2002)
        assert moved.risk_distance == 12
        assert moved.targets[0].rr == pytest.approx(18 / 12)
        assert moved.rr == pytest.approx(28 / 12)
        # Slippage against us reduces RR - which is exactly what the gate must see.
        assert moved.rr < p.rr
