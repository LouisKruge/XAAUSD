"""The positive path: given inputs that satisfy every requirement, a trade is produced,
sized correctly, and carries a complete audit trail.

WHY THIS IS CONSTRUCTED RATHER THAN GENERATED
An earlier version of this test generated price data and hoped it contained a valid
setup. That tests the wrong thing twice over: whether a random walk happens to produce
an A-grade setup is not a property of the system, and when it does not, the test fails
for a reason that has nothing to do with the decision path.

So the snapshot is built directly with every condition the brief requires satisfied,
and the test asserts the pipeline turns it into an executable, correctly sized trade.
Whether real market data contains such setups is a separate question, answered by the
backtester — where an 11-month run produced 5 trades from 24,064 evaluations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xauusd.config.settings import Settings, StrategyThresholds
from xauusd.domain.enums import (
    Bias,
    Classification,
    Direction,
    FVGState,
    Killzone,
    LevelKind,
    LiquidityKind,
    MacroBias,
    NewsRisk,
    OrderBlockKind,
    Regime,
    Session,
    StructureKind,
    SwingKind,
    SwingStrength,
    Timeframe,
    ValidationStatus,
    VolRegime,
    ZoneState,
)
from xauusd.domain.types import (
    FVG,
    AccountState,
    DealingRange,
    LiquidityPool,
    MacroState,
    MarketSnapshot,
    NewsState,
    OrderBlock,
    Quote,
    SessionState,
    SRLevel,
    StructureEvent,
    Sweep,
    Swing,
    SymbolSpec,
    TargetLevel,
    TimeframeStructure,
    TradePlan,
    VolatilityState,
)
from xauusd.execution.broker import BrokerHealth
from xauusd.risk.gate import RiskGate
from xauusd.strategy.classifier import Classifier
from xauusd.strategy.features import extract
from xauusd.strategy.gates import GateContext, run_gates
from xauusd.strategy.scoring import ScoringEngine, reasons_for_and_against

T0 = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)  # Wednesday, London/NY overlap
ENTRY, STOP, TARGET = 2650.0, 2643.0, 2671.0  # 3.0 RR on a 7-point stop


def gold_spec() -> SymbolSpec:
    return SymbolSpec(
        "XAUUSD",
        2,
        0.01,
        100.0,
        0.01,
        1.0,
        1.0,
        1.0,
        0.01,
        50.0,
        0.01,
        10,
        5,
        commission_per_lot=7.0,
    )


def perfect_snapshot(direction: Direction = Direction.LONG, **overrides) -> MarketSnapshot:  # type: ignore[no-untyped-def]
    """A snapshot satisfying every A+ requirement in the brief. Override to weaken it."""
    bullish = direction is Direction.LONG
    bias = Bias.BULLISH if bullish else Bias.BEARISH

    def structure(tf: Timeframe) -> TimeframeStructure:
        mss = StructureEvent(
            ts=T0 - timedelta(minutes=30),
            timeframe=tf,
            kind=StructureKind.MSS,
            direction=direction,
            price=ENTRY - 3,
            break_price=ENTRY + 2,
            displacement_atr=1.6,
            body_ratio=0.82,
        )
        return TimeframeStructure(
            timeframe=tf,
            bias=bias,
            last_event=mss,
            swings=(
                Swing(
                    T0 - timedelta(hours=4), 10, 2665.0, SwingKind.HIGH, tf, SwingStrength.STRONG
                ),
                Swing(T0 - timedelta(hours=2), 20, 2640.0, SwingKind.LOW, tf, SwingStrength.WEAK),
            ),
            dealing_range=DealingRange(2680.0, 2630.0, T0, T0, Timeframe.H4),
            last_mss=mss,
            last_bos=mss,
        )

    swept = LiquidityPool(
        kind=LiquidityKind.EQL if bullish else LiquidityKind.EQH,
        timeframe=Timeframe.M15,
        price=2641.0,
        formed_ts=T0 - timedelta(hours=3),
        touches=3,
        strength=0.85,
        swept_ts=T0 - timedelta(minutes=40),
        sweep_quality=0.9,
    )
    target_pool = LiquidityPool(
        kind=LiquidityKind.PDH if bullish else LiquidityKind.PDL,
        timeframe=Timeframe.D1,
        price=TARGET if bullish else ENTRY - 21.0,
        formed_ts=T0 - timedelta(days=1),
        touches=1,
        strength=0.9,
    )
    sweep = Sweep(
        ts=T0 - timedelta(minutes=40),
        timeframe=Timeframe.M15,
        pool=swept,
        direction=direction,
        penetration=1.4,
        penetration_atr=0.35,
        rejection_ratio=0.72,
        closed_back_inside=True,
        displacement_after_atr=1.5,
        bars_to_reject=1,
    )
    fvg = FVG(
        timeframe=Timeframe.M15,
        direction=direction,
        formed_ts=T0 - timedelta(minutes=25),
        top=ENTRY + 1.5,
        bottom=ENTRY - 1.5,
        size=3.0,
        size_atr=0.6,
        displacement_atr=1.7,
        state=FVGState.UNMITIGATED,
    )
    ob = OrderBlock(
        kind=OrderBlockKind.BULL_OB if bullish else OrderBlockKind.BEAR_OB,
        timeframe=Timeframe.M15,
        direction=direction,
        formed_ts=T0 - timedelta(minutes=35),
        top=ENTRY + 1.2,
        bottom=ENTRY - 1.8,
        open_price=ENTRY + 1.0,
        close_price=ENTRY - 1.5,
        displacement_atr=1.6,
        caused_bos=True,
        swept_liquidity=True,
        has_fvg=True,
        state=ZoneState.FRESH,
    )
    base = dict(
        ts=T0,
        symbol="XAUUSD",
        quote=Quote(T0, ENTRY - 0.1, ENTRY + 0.1),
        structures={
            tf: structure(tf)
            for tf in (
                Timeframe.MN1,
                Timeframe.W1,
                Timeframe.D1,
                Timeframe.H4,
                Timeframe.H1,
                Timeframe.M15,
            )
        },
        liquidity=(swept, target_pool),
        sweeps=(sweep,),
        fvgs=(fvg,),
        order_blocks=(ob,),
        sr_levels=(
            SRLevel(
                kind=LevelKind.SUPPORT if bullish else LevelKind.RESISTANCE,
                timeframe=Timeframe.D1,
                price=ENTRY,
                band_upper=ENTRY + 1,
                band_lower=ENTRY - 1,
                formed_ts=T0 - timedelta(days=5),
                touches=3,
                rejection_strength=0.8,
                importance=0.62,
            ),
        ),
        dealing_range=(
            DealingRange(2680.0, 2630.0, T0, T0, Timeframe.H4)
            if bullish
            else DealingRange(2670.0, 2620.0, T0, T0, Timeframe.H4)
        ),
        session=SessionState(
            session=Session.OVERLAP,
            killzone=Killzone.NY_AM_KZ,
            utc_now=T0,
            london_now=T0,
            ny_now=T0,
            broker_now=T0,
            minutes_into_session=90,
            is_overlap=True,
            is_weekend=False,
            is_holiday=False,
            day_of_week=2,
        ),
        volatility=VolatilityState(
            atr_d1=22.0,
            atr_h4=9.0,
            atr_h1=4.5,
            atr_m15=2.2,
            atr_m5=1.1,
            atr_h1_percentile=0.5,
            realized_vol=0.008,
            vol_regime=VolRegime.NORMAL,
            spread_points=20.0,
            spread_median_points=20.0,
        ),
        regime=Regime.STRONG_BULL if bullish else Regime.STRONG_BEAR,
        macro=MacroState(
            bias=MacroBias.STRONGLY_BULLISH if bullish else MacroBias.STRONGLY_BEARISH,
            dxy_level=103.0,
            dxy_change_1d=-0.2,
            dxy_change_5d=-0.9,
            dxy_trend=Bias.BEARISH if bullish else Bias.BULLISH,
            us10y=4.05,
            us2y=4.35,
            real10y=1.78,
            real10y_change_5d=-0.10,
            breakeven10y=2.34,
            yields_trend=Bias.BEARISH if bullish else Bias.BULLISH,
            curve_10y2y=-0.3,
            as_of=T0,
            is_stale=False,
        ),
        news=NewsState(
            risk=NewsRisk.LOW,
            blackout=False,
            blackout_reason=None,
            blackout_until=None,
            next_event_name="US Jobless Claims",
            next_event_ts=T0 + timedelta(days=2),
            minutes_to_next_event=2880.0,
            directional_hint=Bias.NEUTRAL,
            is_stale=False,
        ),
    )
    base.update(overrides)
    return MarketSnapshot(**base)  # type: ignore[arg-type]


def perfect_plan(direction: Direction = Direction.LONG) -> TradePlan:
    entry, stop, target = (
        (ENTRY, STOP, TARGET) if direction is Direction.LONG else (ENTRY, ENTRY + 7.0, ENTRY - 21.0)
    )
    rr = abs(target - entry) / abs(entry - stop)
    return TradePlan(
        "sweep_mss_fvg",
        "1.0",
        direction,
        entry,
        stop,
        (TargetLevel(target, rr, "PDH liquidity (1 touch)"),),
        T0,
        Timeframe.M15,
        f"a M15 close beyond {stop:.2f} invalidates the structure shift",
        entry_zone_top=entry + 1.5,
        entry_zone_bottom=entry - 1.5,
        symbol="XAUUSD",
        evidence={"fvg_quality": 0.88, "ob_quality": 0.84},
    )


def decide(snap: MarketSnapshot, plan: TradePlan, settings: Settings | None = None):  # type: ignore[no-untyped-def]
    """Run the real composition: hard gates -> scoring -> classification -> risk.

    The hard gates are run for real rather than assumed to pass. Several requirements —
    premium/discount location among them — are enforced ONLY by a gate and not by the
    classifier, so a test that stubs the gates out would silently stop checking them.
    """
    s = settings or Settings(
        thresholds=StrategyThresholds(
            a_score_min=70.0,
            a_plus_score_min=85.0,
            a_strong_categories_min=5,
            a_plus_strong_categories_min=7,
            require_probability_model=False,
        )
    )
    ctx = GateContext(
        settings=s,
        snapshot=snap,
        plan=plan,
        spec=gold_spec(),
        account=AccountState(1, "USD", 10_000.0, 10_000.0, 0.0, 10_000.0, 0.0),
        health=BrokerHealth(True, True, True, 0.5),
        strategy_status=ValidationStatus.OOS_PASSED,
    )
    gates = run_gates(ctx)
    features = extract(snap, plan)
    breakdown = ScoringEngine(s.scoring, s.thresholds, s.news).score(features, snap)
    cls = Classifier(s).classify(
        breakdown=breakdown,
        probability=None,
        features=features,
        snap=snap,
        plan=plan,
        gates_passed=all(g.passed for g in gates),
        strategy_status=ValidationStatus.OOS_PASSED,
    )
    risk = None
    if cls.classification is not Classification.NO_TRADE:
        gate = RiskGate(s)
        gate.drawdown.update(T0, 10_000.0)
        risk = gate.evaluate(
            plan,
            cls.classification,
            AccountState(1, "USD", 10_000.0, 10_000.0, 0.0, 10_000.0, 0.0),
            gold_spec(),
            T0,
        )
    return features, breakdown, cls, risk, gates


class TestAGradeSetupIsAccepted:
    def test_a_perfect_setup_classifies_as_a_trade(self) -> None:
        _, bd, cls, _, _ = decide(perfect_snapshot(), perfect_plan())
        assert cls.classification is not Classification.NO_TRADE, cls.reason
        assert bd.total >= 70.0

    def test_it_scores_strongly_across_independent_categories(self) -> None:
        _, bd, _, _, _ = decide(perfect_snapshot(), perfect_plan())
        assert len(bd.strong_categories) >= 5
        assert sum(bd.penalties.values()) < 3.0

    def test_it_is_sized_within_the_class_cap(self) -> None:
        _, _, cls, risk, _ = decide(perfect_snapshot(), perfect_plan())
        assert risk is not None and risk.approved, risk.reason if risk else "no risk run"
        cap = 0.02 if cls.classification is Classification.A_PLUS else 0.01
        assert risk.sizing.risk_pct <= cap + 1e-9
        assert risk.sizing.lots >= 0.01

    def test_the_short_side_works_identically(self) -> None:
        _, _, cls, risk, _ = decide(
            perfect_snapshot(Direction.SHORT), perfect_plan(Direction.SHORT)
        )
        assert cls.classification is not Classification.NO_TRADE, cls.reason
        assert risk is not None and risk.approved

    def test_it_produces_a_readable_justification(self) -> None:
        snap, plan = perfect_snapshot(), perfect_plan()
        f, bd, _, _, _ = decide(snap, plan)
        for_, _against = reasons_for_and_against(f, bd, snap, plan)
        joined = " ".join(for_)
        assert "sweep" in joined
        assert "structure shift" in joined
        assert "discount" in joined
        assert any("reward-to-risk" in r for r in for_)


class TestWeakeningAnyRequirementBlocksIt:
    """Each of these removes exactly one requirement from an otherwise perfect setup."""

    def _classify(self, **overrides) -> Classification:  # type: ignore[no-untyped-def]
        _, _, cls, _, _ = decide(perfect_snapshot(**overrides), perfect_plan())
        return cls.classification

    def test_higher_timeframe_conflict_blocks(self) -> None:
        snap = perfect_snapshot()
        structures = dict(snap.structures)
        d1 = structures[Timeframe.D1]
        structures[Timeframe.D1] = TimeframeStructure(
            timeframe=Timeframe.D1,
            bias=Bias.BEARISH,
            last_event=d1.last_event,
            swings=d1.swings,
            dealing_range=d1.dealing_range,
            last_mss=d1.last_mss,
        )
        assert self._classify(structures=structures) is Classification.NO_TRADE

    def test_news_blackout_prevents_a_plus(self) -> None:
        blackout = NewsState(
            risk=NewsRisk.HIGH,
            blackout=True,
            blackout_reason="US CPI in 20 min",
            blackout_until=T0 + timedelta(minutes=50),
            next_event_name="US CPI",
            next_event_ts=T0 + timedelta(minutes=20),
            minutes_to_next_event=20.0,
            directional_hint=Bias.NEUTRAL,
            is_stale=False,
        )
        assert self._classify(news=blackout) is not Classification.A_PLUS

    def test_wrong_side_of_the_dealing_range_blocks(self) -> None:
        premium = DealingRange(2655.0, 2600.0, T0, T0, Timeframe.H4)  # entry at 0.91
        assert self._classify(dealing_range=premium) is Classification.NO_TRADE

    def test_no_sweep_prevents_a_plus(self) -> None:
        assert self._classify(sweeps=()) is not Classification.A_PLUS

    def test_unknown_macro_prevents_a_plus(self) -> None:
        from xauusd.core.analyzer import UNKNOWN_MACRO

        assert self._classify(macro=UNKNOWN_MACRO) is not Classification.A_PLUS

    def test_below_the_rr_floor_blocks(self) -> None:
        weak = TradePlan(
            "sweep_mss_fvg",
            "1.0",
            Direction.LONG,
            ENTRY,
            STOP,
            (TargetLevel(ENTRY + 10.0, 10.0 / 7.0, "nearby liquidity"),),
            T0,
            Timeframe.M15,
            "inv",
            symbol="XAUUSD",
        )
        _, _, cls, _, _ = decide(perfect_snapshot(), weak)
        assert cls.classification is Classification.NO_TRADE


class TestTheBrokerCrossCheckIsActuallyWired:
    """PositionSizer refuses to trade when its own loss figure disagrees with the
    broker's `calc_profit`. That is the only thing that catches a symbol specification
    whose tick value does not match its contract size and tick size — a spec that looks
    entirely normal and sizes every position wrongly.

    The cross-check was implemented, tested in isolation, and never called: the pipeline
    invoked the risk gate without passing the broker's figure at all, so it silently did
    nothing for the whole of development.
    """

    def test_the_pipeline_asks_the_broker_what_a_loss_costs(self) -> None:
        from xauusd.engine.pipeline import EngineState

        asked: list[tuple] = []

        def calc(direction, entry, stop):  # type: ignore[no-untyped-def]
            asked.append((direction, entry, stop))
            return -100.0

        state = EngineState(calc_profit=calc)
        plan = perfect_plan()
        from xauusd.engine.pipeline import DecisionPipeline

        got = DecisionPipeline._broker_loss_for_one_lot(state, plan)
        assert got == -100.0
        assert asked == [(plan.direction, plan.entry, plan.stop_loss)]

    def test_no_broker_answer_does_not_block_evaluation(self) -> None:
        """Corroboration, not a precondition: a broker that cannot answer must not stop
        the engine. The sizer already refuses when the answer DISAGREES."""
        from xauusd.engine.pipeline import DecisionPipeline, EngineState

        assert DecisionPipeline._broker_loss_for_one_lot(EngineState(), perfect_plan()) is None

        def explodes(direction, entry, stop):  # type: ignore[no-untyped-def]
            raise RuntimeError("bridge down")

        state = EngineState(calc_profit=explodes)
        assert DecisionPipeline._broker_loss_for_one_lot(state, perfect_plan()) is None
