"""The sizing cross-check must cover BOTH paths to the broker, and must not fail open.

`PositionSizer` compares our loss-per-lot against the broker's own `OrderCalcProfit` and
refuses the trade when they disagree — "refusing to trade on a specification we cannot
verify". It is the only thing standing between a misread contract spec and every
position being sized wrongly.

Two defects, both found by audit while all 680 tests passed (BUG_REGISTER 001, 002):

1. The scalp path passed a literal `None`, so the check protected A/A+ trades and not
   scalp trades — same account, same RiskGate, same broker.
2. A broker that was ASKED and FAILED returned the same `None` as "there is no broker
   to ask", so on real money a broker error silently skipped the check, unlogged.

Both are the same underlying error: an absent value standing for two states that
require opposite responses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Direction, Mode

SRC = Path(__file__).resolve().parents[2] / "src" / "xauusd"


def _settings(mode: Mode) -> Settings:
    return Settings(mode=mode, live_trading=mode is Mode.LIVE)


class _Plan:
    strategy = "sweep_mss_fvg"
    direction = Direction.LONG
    entry = 2000.0
    stop_loss = 1998.0


class _State:
    def __init__(self, calc_profit) -> None:  # type: ignore[no-untyped-def]
        self.calc_profit = calc_profit


class TestNoBrokerIsNotTheSameAsABrokenBroker:
    """The distinction the original `None` collapsed."""

    def test_absent_broker_yields_none_and_does_not_raise(self) -> None:
        """A backtest has no `OrderCalcProfit`. Skipping the check is correct there —
        corroboration that cannot exist is not a precondition."""
        from xauusd.engine.pipeline import DecisionPipeline

        pipe = DecisionPipeline(_settings(Mode.BACKTEST))
        assert pipe._broker_loss_for_one_lot(_State(None), _Plan()) is None

    def test_a_working_broker_is_used(self) -> None:
        from xauusd.engine.pipeline import DecisionPipeline

        pipe = DecisionPipeline(_settings(Mode.BACKTEST))
        state = _State(lambda d, e, s: -123.45)
        assert pipe._broker_loss_for_one_lot(state, _Plan()) == pytest.approx(-123.45)

    def test_a_failing_broker_refuses_on_real_money(self) -> None:
        """The defect: this used to return None, silently skipping the check.

        On real money a broker that cannot price one tick is not one to size positions
        against, and proceeding without the check is exactly the fail-open that
        `CLAUDE.md` forbids.
        """
        from xauusd.engine.pipeline import BrokerCrossCheckUnavailable, DecisionPipeline

        def boom(direction, entry, stop):  # type: ignore[no-untyped-def]
            raise ConnectionError("terminal not responding")

        pipe = DecisionPipeline(_settings(Mode.LIVE))
        with pytest.raises(BrokerCrossCheckUnavailable, match="could not price one lot"):
            pipe._broker_loss_for_one_lot(_State(boom), _Plan())

    @pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER, Mode.DEMO])
    def test_a_failing_broker_degrades_quietly_off_real_money(self, mode: Mode) -> None:
        """Outside real money the original reasoning holds: keep evaluating.

        The engine must still be usable for research when a simulator misbehaves.
        """
        from xauusd.engine.pipeline import DecisionPipeline

        def boom(direction, entry, stop):  # type: ignore[no-untyped-def]
            raise ConnectionError("terminal not responding")

        pipe = DecisionPipeline(_settings(mode))
        assert pipe._broker_loss_for_one_lot(_State(boom), _Plan()) is None


class TestTheScalpPathHasTheSameCheck:
    def test_the_scalp_pipeline_accepts_a_calc_profit_callable(self) -> None:
        """It must be a CALLABLE, not one value shared across a cycle.

        Every candidate has its own entry and stop, so a single value would compare
        against the wrong trade — worse than no cross-check, because it would still
        look like verification.
        """
        import inspect

        from xauusd.engine.scalp_pipeline import ScalpPipeline

        params = inspect.signature(ScalpPipeline.run).parameters
        assert "calc_profit" in params, "the scalp path must take a broker cross-check"
        assert "broker_calc_profit" not in params, (
            "a single per-cycle value is wrong: entry and stop differ per signal"
        )

    def test_the_live_scalp_scan_supplies_a_real_broker_call(self) -> None:
        """The literal `None` that caused BUG-002, pinned at the source.

        Read from the parsed orchestrator rather than by string search, so a comment
        about the bug cannot be mistaken for the bug being fixed.
        """
        tree = ast.parse((SRC / "engine" / "orchestrator.py").read_text(encoding="utf-8"))
        scalp_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "scalp"
        ]
        assert scalp_calls, "the orchestrator no longer runs the scalp pipeline"
        for call in scalp_calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "calc_profit" in kw, "the live scalp scan must pass a broker cross-check"
            assert not (
                isinstance(kw["calc_profit"], ast.Constant) and kw["calc_profit"].value is None
            ), "calc_profit=None disables the cross-check on live scalp trades"

    def test_a_failing_broker_refuses_a_scalp_on_real_money(self) -> None:
        from xauusd.engine.pipeline import BrokerCrossCheckUnavailable
        from xauusd.engine.scalp_pipeline import ScalpPipeline
        from xauusd.strategy.scalp.base import ScalpFactors, ScalpSignal

        signal = ScalpSignal(
            model="scalp_sweep_reversal",
            version="1.0.0",
            direction=Direction.LONG,
            entry=2000.0,
            stop_loss=1998.0,
            target=2003.0,
            ts=__import__("datetime").datetime.now(__import__("datetime").UTC),
            factors=ScalpFactors(),
        )

        def boom(direction, entry, stop):  # type: ignore[no-untyped-def]
            raise ConnectionError("terminal not responding")

        pipe = ScalpPipeline(_settings(Mode.LIVE))
        with pytest.raises(BrokerCrossCheckUnavailable):
            pipe._broker_loss_for_one_lot(boom, signal)

    def test_the_scalp_check_prices_the_signals_own_levels(self) -> None:
        """It must ask about THIS signal's entry and stop, not some other trade's."""
        from datetime import UTC, datetime

        from xauusd.engine.scalp_pipeline import ScalpPipeline
        from xauusd.strategy.scalp.base import ScalpFactors, ScalpSignal

        seen: list[tuple] = []

        def record(direction, entry, stop):  # type: ignore[no-untyped-def]
            seen.append((direction, entry, stop))
            return -50.0

        signal = ScalpSignal(
            model="scalp_ob_reaction",
            version="1.0.0",
            direction=Direction.SHORT,
            entry=2011.5,
            stop_loss=2014.25,
            target=2005.0,
            ts=datetime.now(UTC),
            factors=ScalpFactors(),
        )
        pipe = ScalpPipeline(_settings(Mode.BACKTEST))
        assert pipe._broker_loss_for_one_lot(record, signal) == pytest.approx(-50.0)
        assert seen == [(Direction.SHORT, 2011.5, 2014.25)]
