"""The scalp models: micro structure, signal geometry, and the scorer.

The bug these were written after: FVG retracement and order-block reaction produced
exactly zero signals across 998 scan instants, and it looked like thin data. It was
not. Entry and stop were placed on the SAME edge of the zone, so the stop distance was
the buffer alone — a constant 0.30 ATR, always under the 0.8 ATR floor, rejected 79
times out of 79 by construction.

A model that cannot fire looks identical to a market with no setups, which is the
single most expensive confusion in this project. So the geometry is pinned here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fixtures.synthetic import market_m1
from xauusd.config.settings import ScalpScoreWeights, Settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import Direction, Timeframe
from xauusd.domain.types import Quote, SymbolSpec
from xauusd.strategy.scalp.base import ScalpFactors, ScalpRegistry, structural_target
from xauusd.strategy.scalp.models import default_scalp_registry
from xauusd.strategy.scalp_score import ScalpScorer

SPEC = SymbolSpec("XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)


@pytest.fixture(scope="module")
def scanned():
    """Every signal five models produce over a synthetic M1 market."""
    settings = Settings()
    data = market_m1(30_000, seed=9)
    source = InMemoryBarSource(data)
    macro, micro_an = MarketAnalyzer(settings), MicroAnalyzer(settings)
    registry = default_scalp_registry(settings)

    m1 = data[Timeframe.M1]
    signals: list = []
    usable = 0
    for i in range(2000, len(m1), 20):
        bar = m1.bar_at(i)
        now = bar.ts + timedelta(seconds=60)
        half = (bar.spread_points or 25) * SPEC.point / 2
        view = MarketView(source, "XAUUSD", now, Quote(now, bar.close - half, bar.close + half))
        micro = micro_an.analyze(view)
        if not micro.usable:
            continue
        usable += 1
        snap = macro.analyze(view, None, None, float(bar.spread_points or 25), 25.0)
        for model in registry.all():
            signals.extend(model.detect(micro, snap))
    return settings, usable, signals


class TestEveryModelCanActuallyFire:
    """A model that never fires is indistinguishable from a quiet market."""

    def test_the_scan_produced_usable_instants(self, scanned) -> None:
        _, usable, _ = scanned
        assert usable > 500, "the fixture must warm up and stay usable"

    def test_signals_were_produced(self, scanned) -> None:
        _, _, signals = scanned
        assert signals, "no model produced a single signal"

    @pytest.mark.parametrize(
        "model_name",
        [
            "scalp_sweep_reversal",
            "scalp_fvg_retracement",
            "scalp_ob_reaction",
            "scalp_breakout_retest",
            "scalp_momentum_continuation",
        ],
    )
    def test_each_model_fires_at_least_once(self, scanned, model_name: str) -> None:
        _, _, signals = scanned
        produced = [s for s in signals if s.model == model_name]
        assert produced, (
            f"{model_name} produced no signal in the whole scan. Either the pattern is "
            f"absent from this fixture or the model cannot fire at all — and those need "
            f"opposite responses, so this must be investigated, not relaxed."
        )


class TestSignalGeometry:
    """The bug that motivated the file."""

    def test_the_stop_is_never_merely_the_buffer(self, scanned) -> None:
        """Entry and stop on the same zone edge give a stop distance of the buffer
        alone: meaningless as invalidation, and far too tight to survive costs."""
        settings, _, signals = scanned
        for s in signals:
            assert s.stop_distance > 0, f"{s.model} produced a zero-width stop"

    def test_every_stop_is_within_the_configured_atr_band(self, scanned) -> None:
        """Too tight and the cost gate rejects it anyway; too wide and it is an
        intraday swing wearing a scalp's holding time."""
        settings, _, signals = scanned
        cfg = settings.scalp
        for s in signals:
            stop_atr = float(s.evidence["stop_atr"])
            assert cfg.min_stop_atr <= stop_atr <= cfg.max_stop_atr, (
                f"{s.model}: stop of {stop_atr:.2f} ATR is outside "
                f"[{cfg.min_stop_atr}, {cfg.max_stop_atr}]"
            )

    def test_the_atr_the_stop_was_scaled_by_is_recorded(self, scanned) -> None:
        """Every structural threshold is ATR-scaled, so a journal entry without the ATR
        cannot be re-derived later."""
        _, _, signals = scanned
        for s in signals:
            assert s.evidence["atr_m5"] > 0

    def test_stops_sit_on_the_correct_side_of_entry(self, scanned) -> None:
        _, _, signals = scanned
        for s in signals:
            if s.direction is Direction.LONG:
                assert s.stop_loss < s.entry, f"{s.model}: long stop above entry"
            else:
                assert s.stop_loss > s.entry, f"{s.model}: short stop below entry"

    def test_targets_sit_on_the_profitable_side(self, scanned) -> None:
        _, _, signals = scanned
        for s in signals:
            if s.direction is Direction.LONG:
                assert s.target > s.entry, f"{s.model}: long target below entry"
            else:
                assert s.target < s.entry, f"{s.model}: short target above entry"

    def test_every_signal_converts_to_a_valid_trade_plan(self, scanned) -> None:
        """TradePlan validates stop side and target presence at construction, so this
        is the end-to-end check that a signal can reach the existing risk path."""
        _, _, signals = scanned
        for s in signals:
            plan = s.to_plan("XAUUSD")
            assert plan.symbol == "XAUUSD"
            assert plan.targets
            assert plan.strategy == s.model


class TestTargetPlacement:
    def test_an_obstacle_pulls_the_target_in(self) -> None:
        """Reaching through resting liquidity is how a 1:1.5 becomes a 1:0.8 fill."""
        target, why = structural_target(
            entry=100.0,
            stop=98.0,
            direction=Direction.LONG,
            target_rr=1.5,
            obstacles=[101.5],
        )
        assert target == 101.5
        assert "opposing level at" in why

    def test_no_obstacle_gives_the_requested_rr(self) -> None:
        target, why = structural_target(100.0, 98.0, Direction.LONG, 1.5, [])
        assert target == pytest.approx(103.0)
        assert "no obstacle" in why

    def test_obstacles_behind_the_entry_are_ignored(self) -> None:
        target, _ = structural_target(100.0, 98.0, Direction.LONG, 1.5, [97.0, 99.0])
        assert target == pytest.approx(103.0)

    def test_a_short_target_looks_downward(self) -> None:
        target, _ = structural_target(100.0, 102.0, Direction.SHORT, 1.5, [98.5])
        assert target == 98.5

    def test_an_obstacle_inside_the_noise_floor_is_traded_through(self) -> None:
        """The liquidity engine finds 150+ pools on a few hundred M5 bars, so there is
        almost always one within a cent of price. Respecting it produced targets of
        0.01R — not a conservative target, no target at all."""
        target, why = structural_target(
            entry=100.0,
            stop=98.0,
            direction=Direction.LONG,
            target_rr=1.5,
            obstacles=[100.02],
            min_rr=0.75,
        )
        assert target == pytest.approx(103.0), "a level 0.01R away is noise, not a barrier"
        assert "beyond the 0.75R floor" in why

    def test_an_obstacle_just_past_the_floor_is_respected(self) -> None:
        target, _ = structural_target(100.0, 98.0, Direction.LONG, 1.5, [101.6], min_rr=0.75)
        assert target == 101.6

    def test_a_zero_risk_plan_cannot_produce_a_target(self) -> None:
        target, why = structural_target(100.0, 100.0, Direction.LONG, 1.5, [])
        assert target == 100.0
        assert "no risk distance" in why


class TestTheScorer:
    def test_a_perfect_signal_scores_one_hundred(self) -> None:
        perfect = ScalpFactors(**dict.fromkeys(ScalpFactors().as_dict(), 1.0))
        assert ScalpScorer().score(perfect).total == pytest.approx(100.0)

    def test_an_empty_signal_scores_zero(self) -> None:
        assert ScalpScorer().score(ScalpFactors()).total == pytest.approx(0.0)

    def test_a_nan_factor_cannot_poison_the_total(self) -> None:
        """A NaN would compare false against every threshold and reject the signal for
        no stated reason. Warm-up must reject loudly, not invisibly."""
        score = ScalpScorer().score(ScalpFactors(momentum=float("nan"), liquidity=1.0))
        assert score.total == score.total, "score must not be NaN"
        assert score.total == pytest.approx(20.0)

    def test_weights_must_total_one_hundred(self) -> None:
        with pytest.raises(ValueError, match="must total 100"):
            ScalpScoreWeights(market_structure=50.0)

    def test_the_score_names_its_weakest_factors(self) -> None:
        """The journal's answer to 'why only 58?'"""
        score = ScalpScorer().score(
            ScalpFactors(market_structure=1.0, liquidity=1.0, momentum=0.0, news=0.0)
        )
        assert "market_structure" in score.strongest
        assert "momentum" in score.weakest


class TestTheRegistry:
    def test_all_five_models_register(self) -> None:
        assert len(default_scalp_registry()) == 5

    def test_a_duplicate_name_is_refused(self) -> None:
        r = ScalpRegistry()
        model = default_scalp_registry().all()[0]
        r.register(model)
        with pytest.raises(ValueError, match="duplicate"):
            r.register(model)

    def test_nothing_is_enabled_by_default(self) -> None:
        """A model earns its way into enabled_models by clearing validation."""
        assert Settings().scalp.enabled_models == []
        assert default_scalp_registry().enabled(Settings().scalp.enabled_models) == []
