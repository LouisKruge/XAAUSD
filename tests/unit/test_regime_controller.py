"""The Market Regime Controller (spec §31) — the layer that decides which engine trades.

Its one dangerous property is the one most worth pinning: **it may only ever remove
permission.** §31 says "the controller should never override hard safety rules", so a
verdict of BOTH must not be able to unlock anything the risk engine, kill switch or
drawdown guard would refuse. It is a veto, never a licence.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Bias, NewsRisk, Regime, Timeframe, VolRegime
from xauusd.engine.regime_controller import (
    TRENDING,
    EnginePermission,
    MarketRegimeController,
    RegimeVerdict,
)


@pytest.fixture(scope="module")
def base_snapshot():
    """A real snapshot, not a hand-rolled double, for the reasons in FINDINGS 34."""
    from datetime import timedelta

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


def _snap(base, **over):  # type: ignore[no-untyped-def]
    """Override just the fields the controller reads."""
    vol_over = {k[4:]: v for k, v in over.items() if k.startswith("vol_")}
    news_over = {k[5:]: v for k, v in over.items() if k.startswith("news_")}
    rest = {k: v for k, v in over.items() if not k.startswith(("vol_", "news_"))}
    snap = base
    if vol_over:
        snap = replace(snap, volatility=replace(snap.volatility, **vol_over))
    if news_over:
        snap = replace(snap, news=replace(snap.news, **news_over))
    return replace(snap, **rest) if rest else snap


class TestItCanOnlyRemovePermission:
    def test_every_permission_is_a_subset_of_both(self) -> None:
        """BOTH is the ceiling. No verdict can allow more than both engines running."""
        for p in EnginePermission:
            assert not (p.scalp_allowed and not EnginePermission.BOTH.scalp_allowed)
            assert not (p.intraday_allowed and not EnginePermission.BOTH.intraday_allowed)

    def test_adding_a_blocking_condition_never_adds_permission(self, base_snapshot) -> None:
        """Property test: each condition, applied alone, may only take permission away.

        The baseline must be genuinely CLEAN. A first version compared against the raw
        fixture, which already had LOW volatility blocking scalping — so setting EXTREME
        *replaced* that block instead of adding one, and the test read a legitimate
        swap as the controller granting permission. Comparing a changed condition
        against a baseline that was already blocked for a different reason measures
        nothing.
        """
        ctrl = MarketRegimeController(Settings())
        clean_snap = _snap(
            base_snapshot,
            regime=Regime.STRONG_BULL,
            vol_vol_regime=VolRegime.NORMAL,
            vol_spread_points=25.0,
            vol_spread_median_points=25.0,
            news_blackout=False,
            news_risk=NewsRisk.LOW,
        )
        clean = ctrl.evaluate(clean_snap)
        assert clean.permission is EnginePermission.BOTH, (
            f"the baseline must start unblocked or the property is untestable: "
            f"scalp={clean.scalp_reasons} intraday={clean.intraday_reasons}"
        )

        worse = [
            _snap(clean_snap, news_blackout=True, news_blackout_reason="CPI"),
            _snap(clean_snap, vol_vol_regime=VolRegime.LOW),
            _snap(clean_snap, vol_vol_regime=VolRegime.EXTREME),
            _snap(clean_snap, news_risk=NewsRisk.EXTREME),
            _snap(clean_snap, regime=Regime.RANGE),
            _snap(clean_snap, vol_spread_points=300.0, vol_spread_median_points=25.0),
        ]
        for s in worse:
            v = ctrl.evaluate(s)
            assert not (v.scalp_allowed and not clean.scalp_allowed), (
                "a blocking condition granted scalp permission it did not have"
            )
            assert not (v.intraday_allowed and not clean.intraday_allowed), (
                "a blocking condition granted intraday permission it did not have"
            )


class TestConditionsThatStopBothEngines:
    def test_a_news_blackout_stops_everything(self, base_snapshot) -> None:
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, news_blackout=True, news_blackout_reason="FOMC")
        )
        assert v.permission is EnginePermission.NEITHER
        assert "FOMC" in v.why_not("scalp")
        assert "FOMC" in v.why_not("intraday")

    def test_an_abnormal_spread_stops_everything(self, base_snapshot) -> None:
        """§32: widened spread plus abnormal volatility means no new risk."""
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, vol_spread_points=200.0, vol_spread_median_points=25.0)
        )
        assert v.permission is EnginePermission.NEITHER
        assert "spread" in v.why_not("scalp")

    def test_a_normal_spread_does_not(self, base_snapshot) -> None:
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, vol_spread_points=26.0, vol_spread_median_points=25.0)
        )
        assert "spread" not in v.why_not("scalp")


class TestTheEnginesAreBlockedForDifferentReasons:
    """The whole point of the layer: their hypotheses fail in different conditions."""

    def test_a_range_stops_intraday_but_not_scalping(self, base_snapshot) -> None:
        """§20: do not force a trend trade in a range. A snapback does not care."""
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, regime=Regime.RANGE, vol_vol_regime=VolRegime.NORMAL)
        )
        assert not v.intraday_allowed
        assert "not directional" in v.why_not("intraday")
        assert v.scalp_allowed

    def test_a_dead_market_stops_scalping_but_may_leave_intraday(self, base_snapshot) -> None:
        """A snapback needs a dislocation to snap back from."""
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, regime=Regime.STRONG_BULL, vol_vol_regime=VolRegime.LOW)
        )
        assert not v.scalp_allowed
        assert "LOW" in v.why_not("scalp")

    def test_extreme_volatility_stops_intraday_pullback_entries(self, base_snapshot) -> None:
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, regime=Regime.STRONG_BULL, vol_vol_regime=VolRegime.EXTREME)
        )
        assert not v.intraday_allowed
        assert "EXTREME" in v.why_not("intraday")

    def test_high_news_risk_stops_scalping(self, base_snapshot) -> None:
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, news_risk=NewsRisk.HIGH)
        )
        assert not v.scalp_allowed
        assert "news risk" in v.why_not("scalp")

    def test_a_range_is_not_in_the_trending_set(self) -> None:
        """§20, pinned directly: RANGE must never count as a directional regime."""
        assert Regime.RANGE not in TRENDING
        assert Regime.STRONG_BULL in TRENDING
        assert Regime.MODERATE_BEAR in TRENDING


class TestTimeframeConflictStopsIntraday:
    def test_h4_against_h1_blocks_the_trend_engine(self, base_snapshot) -> None:
        """§21: 4H bullish with 1H bearish means NO intraday trade, stated as a rule."""
        from xauusd.domain.types import TimeframeStructure

        def struct(tf, bias):  # type: ignore[no-untyped-def]
            return TimeframeStructure(
                timeframe=tf, bias=bias, last_event=None, swings=(), dealing_range=None
            )

        conflicted = replace(
            base_snapshot,
            regime=Regime.STRONG_BULL,
            structures={
                **base_snapshot.structures,
                Timeframe.H4: struct(Timeframe.H4, Bias.BULLISH),
                Timeframe.H1: struct(Timeframe.H1, Bias.BEARISH),
            },
        )
        v = MarketRegimeController(Settings()).evaluate(conflicted)
        assert not v.intraday_allowed
        assert "conflicts" in v.why_not("intraday")

    def test_agreement_does_not_block(self, base_snapshot) -> None:
        from xauusd.domain.types import TimeframeStructure

        def struct(tf, bias):  # type: ignore[no-untyped-def]
            return TimeframeStructure(
                timeframe=tf, bias=bias, last_event=None, swings=(), dealing_range=None
            )

        agreed = replace(
            base_snapshot,
            structures={
                **base_snapshot.structures,
                Timeframe.H4: struct(Timeframe.H4, Bias.BULLISH),
                Timeframe.H1: struct(Timeframe.H1, Bias.BULLISH),
            },
        )
        assert "conflicts" not in MarketRegimeController(Settings()).evaluate(agreed).why_not(
            "intraday"
        )


class TestTheVerdictExplainsItself:
    def test_a_blocked_verdict_carries_its_reasons(self, base_snapshot) -> None:
        """ "Not trading" and "not trading because X" are different facts, and only the
        second is actionable. This project has twice paid for the difference."""
        v = MarketRegimeController(Settings()).evaluate(
            _snap(base_snapshot, news_blackout=True, news_blackout_reason="NFP")
        )
        assert v.why_not("scalp")
        assert v.as_dict()["scalp_blocked_by"]

    def test_an_unblocked_verdict_has_nothing_to_explain(self) -> None:
        v = RegimeVerdict(permission=EnginePermission.BOTH, regime=Regime.STRONG_BULL)
        assert v.why_not("scalp") == ""
        assert v.why_not("intraday") == ""
