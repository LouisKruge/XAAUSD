"""An unvalidated scalp model must not be able to reach real money.

`g_strategy_validated` has guarded the A/A+ chain since the beginning: a strategy whose
`ValidationStatus` is not `OOS_PASSED` or better cannot route in LIVE mode. The scalp
pipeline was built as a parallel path to the same broker — same `RiskGate`, same sizing,
same caps — and it did not run that check. So the system's state was:

    A/A+ strategy, status DEV, LIVE mode   -> refused
    scalp model,   status DEV, LIVE mode   -> routed

and every scalp model ships DEV. This is the same defect class as FINDINGS 38: one rule,
several enforcement points, and the newest path never learned the rule.

These tests assert the behaviour is *impossible*, not merely absent — the brief's
standard for anything that can cost money.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.fixtures.synthetic import market_m1
from xauusd.config.settings import Settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import Direction, Mode, Timeframe, ValidationStatus
from xauusd.domain.types import AccountState, Quote, SymbolSpec
from xauusd.engine.scalp_pipeline import ScalpPipeline
from xauusd.strategy.scalp.base import ScalpFactors, ScalpSignal

SPEC = SymbolSpec("GOLD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)
MODELS = [
    "scalp_sweep_reversal",
    "scalp_fvg_retracement",
    "scalp_ob_reaction",
    "scalp_breakout_retest",
    "scalp_momentum_continuation",
]


class _AlwaysFires:
    """A model that emits one flawless signal every time.

    Deliberately perfect: it clears the score, the reward-to-risk floor and the economic
    gates, so the ONLY thing that can refuse it is the validation check. A test whose
    signal could be rejected for another reason would pass whether or not the check
    exists.
    """

    class meta:
        name = "scalp_sweep_reversal"
        version = "1.0.0"

    def detect(self, micro, snap):  # type: ignore[no-untyped-def]
        entry = snap.quote.mid
        risk = 2.0 * micro.atr_m5
        return [
            ScalpSignal(
                model=self.meta.name,
                version=self.meta.version,
                direction=Direction.LONG,
                entry=entry,
                stop_loss=entry - risk,
                target=entry + risk * 3.0,
                ts=micro.ts,
                factors=ScalpFactors(**dict.fromkeys(ScalpFactors().as_dict(), 1.0)),
                evidence={},
            )
        ]


class _Registry:
    def __init__(self, model) -> None:  # type: ignore[no-untyped-def]
        self._model = model

    def enabled(self, names):  # type: ignore[no-untyped-def]
        return [self._model]

    def all(self):  # type: ignore[no-untyped-def]
        return [self._model]


@pytest.fixture(scope="module")
def world():
    """One usable micro/market snapshot pair, built the way the engine builds them."""
    settings = Settings()
    data = market_m1(12_000, seed=7)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]
    macro, micro_an = MarketAnalyzer(settings), MicroAnalyzer(settings)
    for i in range(len(m1) - 2, 3000, -50):
        bar = m1.bar_at(i)
        now = bar.ts + timedelta(seconds=60)
        view = MarketView(source, "GOLD", now, Quote(now, bar.close - 0.05, bar.close + 0.05))
        micro = micro_an.analyze(view)
        if micro.usable:
            return micro, macro.analyze(view, None, None, 25.0, 25.0), now
    pytest.fail("no usable micro snapshot in the fixture")


def _account(now: datetime) -> AccountState:
    return AccountState(
        login=1,
        currency="USD",
        balance=10_000.0,
        equity=10_000.0,
        margin=0.0,
        free_margin=10_000.0,
        margin_level=0.0,
        ts=now,
    )


def _settings(mode: Mode) -> Settings:
    """Scalp enabled explicitly: `Settings()` ships it off, and a pipeline that
    short-circuits on `enabled` would make every assertion below vacuously true."""
    # LIVE also requires live_trading=true — the two-key arming the config enforces.
    base = Settings(mode=mode, live_trading=mode is Mode.LIVE)
    return base.model_copy(
        update={
            "scalp": base.scalp.model_copy(
                update={"enabled": True, "enabled_models": MODELS, "min_score": 0.0}
            )
        }
    )


def _run(mode: Mode, status: dict[str, ValidationStatus], world):  # type: ignore[no-untyped-def]
    micro, snap, now = world
    pipe = ScalpPipeline(_settings(mode), registry=_Registry(_AlwaysFires()))
    return pipe.run(
        micro,
        snap,
        account=_account(now),
        spec=SPEC,
        now=now,
        strategy_status=status,
    )


class TestLiveRoutingRequiresValidation:
    @pytest.mark.parametrize("model", MODELS)
    def test_no_shipped_model_carries_its_own_validation_status(self, model: str) -> None:
        """Validation status lives in the database, never in the model's own metadata.

        A model that declared its own status could declare itself validated, which is
        the whole point of keeping the record somewhere the code cannot write to on a
        whim. This pins that separation rather than assuming it.
        """
        from xauusd.strategy.scalp.models import default_scalp_registry

        meta = next(
            m.meta for m in default_scalp_registry(Settings()).all() if m.meta.name == model
        )
        assert not hasattr(meta, "status")

    def test_a_dev_model_is_refused_in_live_mode(self, world) -> None:
        cycle = _run(Mode.LIVE, {"scalp_sweep_reversal": ValidationStatus.DEV}, world)
        assert cycle.executable is None
        assert [e.rejected_by for e in cycle.evaluations] == ["scalp_strategy_validated"]

    def test_an_unknown_model_is_refused_in_live_mode(self, world) -> None:
        """No database row means no evidence of validation, which is not the same as
        evidence of validity. Absence must read as DEV, never as permission."""
        cycle = _run(Mode.LIVE, {}, world)
        assert cycle.executable is None
        assert [e.rejected_by for e in cycle.evaluations] == ["scalp_strategy_validated"]

    @pytest.mark.parametrize(
        "status",
        [ValidationStatus.IN_SAMPLE_PASSED, ValidationStatus.FAILED, ValidationStatus.RETIRED],
    )
    def test_every_non_eligible_status_is_refused(self, world, status: ValidationStatus) -> None:
        cycle = _run(Mode.LIVE, {"scalp_sweep_reversal": status}, world)
        assert cycle.executable is None

    @pytest.mark.parametrize(
        "status",
        [
            ValidationStatus.OOS_PASSED,
            ValidationStatus.PAPER,
            ValidationStatus.DEMO,
            ValidationStatus.LIVE,
        ],
    )
    def test_a_validated_model_is_allowed_through_this_gate(
        self, world, status: ValidationStatus
    ) -> None:
        """The gate must not simply refuse everything — that would pass every test above
        while quietly disabling the engine."""
        cycle = _run(Mode.LIVE, {"scalp_sweep_reversal": status}, world)
        names = [c.name for e in cycle.evaluations for c in e.checks]
        assert "scalp_strategy_validated" in names
        assert all(e.rejected_by != "scalp_strategy_validated" for e in cycle.evaluations), (
            "a validated strategy was refused by the validation gate"
        )


class TestBacktestingIsNotBlocked:
    def test_a_dev_model_still_runs_outside_live_mode(self, world) -> None:
        """Validation is how a strategy earns live routing, so refusing DEV models in a
        backtest would make the status unreachable: nothing could ever be validated."""
        cycle = _run(Mode.BACKTEST, {"scalp_sweep_reversal": ValidationStatus.DEV}, world)
        assert all(e.rejected_by != "scalp_strategy_validated" for e in cycle.evaluations)


class TestTheCheckIsRecordedEvenWhenItPasses:
    def test_the_journal_carries_the_status_either_way(self, world) -> None:
        """The record is the product. A gate that only appears when it fires leaves the
        journal unable to show that it was consulted."""
        cycle = _run(Mode.BACKTEST, {"scalp_sweep_reversal": ValidationStatus.DEV}, world)
        check = next(
            c for e in cycle.evaluations for c in e.checks if c.name == "scalp_strategy_validated"
        )
        assert str(ValidationStatus.DEV) in str(check.observed)
