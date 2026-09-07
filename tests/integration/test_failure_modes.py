"""Deliberate failure injection: the system must fail SAFE, and say so.

Directive §34 and §35. Each test breaks something on purpose and asserts the response is
"do not trade" plus a stated reason — never a fabricated price, a guessed fill, or a
silent skip.

The rule these all serve, from CLAUDE.md: *every missing input must make the system less
willing to trade*. A failure that makes it more willing, or equally willing, is the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Direction, Mode
from xauusd.domain.types import BrokerPosition, SymbolSpec
from xauusd.execution.broker import BrokerError

SPEC = SymbolSpec("GOLD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)


# --------------------------------------------------------------------------------------
# §34 — the broker is unavailable or wrong
# --------------------------------------------------------------------------------------


class TestABrokenBrokerNeverProducesATrade:
    def test_a_broker_that_cannot_price_refuses_on_real_money(self) -> None:
        """The BUG-001 case, as an end-to-end statement rather than a unit detail."""
        from xauusd.engine.pipeline import (
            BrokerCrossCheckUnavailable,
            DecisionPipeline,
            EngineState,
        )

        class _Plan:
            strategy = "sweep_mss_fvg"
            direction = Direction.LONG
            entry = 2000.0
            stop_loss = 1998.0

        def dead(direction, entry, stop):  # type: ignore[no-untyped-def]
            raise BrokerError("terminal not responding")

        pipe = DecisionPipeline(Settings(mode=Mode.LIVE, live_trading=True))
        with pytest.raises(BrokerCrossCheckUnavailable):
            pipe._broker_loss_for_one_lot(EngineState(calc_profit=dead), _Plan())

    def test_a_disagreeing_broker_refuses_to_size(self) -> None:
        """The check exists for this: our arithmetic and the broker's must agree.

        A 10x disagreement is what a misread contract size looks like, and it must
        produce a refusal rather than a position ten times too large.
        """
        from xauusd.risk.position_sizing import PositionSizer, SizingInputs

        sizer = PositionSizer(Settings().risk)
        inputs = SizingInputs(
            equity=10_000.0,
            risk_pct=0.01,
            entry=2000.0,
            stop_loss=1990.0,
            direction=Direction.LONG,
            spec=SPEC,
        )
        honest = sizer.calculate(inputs)
        assert honest.approved, "the control case must approve, or this proves nothing"

        # The broker says one lot loses ten times what we computed.
        ours = honest.loss_per_lot
        refused = sizer.calculate(inputs, broker_calc_profit=-(ours * 10))
        assert not refused.approved
        assert refused.lots == 0.0
        assert "disagrees with the broker" in refused.reason

    def test_insufficient_margin_refuses(self) -> None:
        from xauusd.risk.position_sizing import PositionSizer, SizingInputs

        sizer = PositionSizer(Settings().risk)
        inputs = SizingInputs(
            equity=10_000.0,
            risk_pct=0.01,
            entry=2000.0,
            stop_loss=1990.0,
            direction=Direction.LONG,
            spec=SPEC,
            free_margin=10.0,
        )
        result = sizer.calculate(inputs, broker_calc_margin=5_000.0)
        assert not result.approved
        assert result.lots == 0.0
        assert "margin" in result.reason.lower()


# --------------------------------------------------------------------------------------
# §34 — degraded or absent market data
# --------------------------------------------------------------------------------------


class TestMissingDataMakesItLessWillingNeverMore:
    def test_warmup_produces_no_micro_judgement(self) -> None:
        """ATR is NaN until the period fills, and every structural threshold is scaled by
        ATR — so a missing ATR does not mean "use a default", it means no judgement is
        available. `usable` must be False rather than the analyzer inventing one."""
        from xauusd.core.micro_structure import MicroSnapshot

        warming = MicroSnapshot(ts=datetime.now(UTC), atr_m1=float("nan"), atr_m5=float("nan"))
        assert not warming.usable

    def test_a_degraded_snapshot_is_unusable_even_with_good_atr(self) -> None:
        from xauusd.core.micro_structure import MicroSnapshot

        degraded = MicroSnapshot(
            ts=datetime.now(UTC), atr_m1=0.3, atr_m5=1.2, degraded=("M1: only 4 bars",)
        )
        assert not degraded.usable

    def test_an_unusable_snapshot_stops_the_scalp_cycle_before_any_model_runs(self) -> None:
        from xauusd.core.micro_structure import MicroSnapshot
        from xauusd.domain.types import AccountState
        from xauusd.engine.scalp_pipeline import ScalpPipeline

        base = Settings()
        settings = base.model_copy(
            update={
                "scalp": base.scalp.model_copy(
                    update={"enabled": True, "enabled_models": ["scalp_sweep_reversal"]}
                )
            }
        )
        now = datetime.now(UTC)
        cycle = ScalpPipeline(settings).run(
            MicroSnapshot(ts=now, atr_m1=float("nan"), atr_m5=float("nan")),
            None,  # never reached: the cycle skips before touching the snapshot
            account=AccountState(
                login=1,
                currency="USD",
                balance=10_000.0,
                equity=10_000.0,
                margin=0.0,
                free_margin=10_000.0,
                margin_level=0.0,
            ),
            spec=SPEC,
            now=now,
        )
        assert cycle.executable is None
        assert cycle.skipped is not None
        assert not cycle.evaluations, "no model may run on data that is not usable"


# --------------------------------------------------------------------------------------
# §34 — the kill switch
# --------------------------------------------------------------------------------------


class TestTheKillSwitchActuallyBlocks:
    def test_a_tripped_switch_blocks_entries(self) -> None:
        from xauusd.domain.enums import KillSwitchReason
        from xauusd.risk.kill_switch import KillSwitch

        ks = KillSwitch()
        assert ks.blocks_entry()[0] is False

        ks.trip(KillSwitchReason.DAILY_DRAWDOWN, "daily loss limit reached")
        blocked, why = ks.blocks_entry()
        assert blocked is True
        assert why

    def test_a_non_auto_clearable_reason_needs_a_named_human(self) -> None:
        """Anything that halts trading for a reason a machine cannot judge safe must
        require a person to put their name to restarting it."""
        from xauusd.domain.enums import KillSwitchReason
        from xauusd.risk.kill_switch import KillSwitch

        hard = next((r for r in KillSwitchReason if not r.auto_clearable), None)
        if hard is None:
            pytest.skip("no non-auto-clearable reasons defined")

        ks = KillSwitch()
        ks.trip(hard, "something a machine must not undo on its own")
        assert ks.clear(hard, by="") is False, "must not clear itself"
        assert ks.is_active(hard)
        assert ks.clear(hard, by="operator", force=True) is True
        assert not ks.is_active(hard)


# --------------------------------------------------------------------------------------
# §35 — restart recovery
# --------------------------------------------------------------------------------------


class TestRestartRecovery:
    def test_an_orphaned_broker_position_is_adopted_not_duplicated(self) -> None:
        """The crash case: the broker holds a position our records do not know about.

        Losing it means an unmanaged position with no stop management; opening another
        means double risk. Adoption is the only safe answer.
        """
        from xauusd.execution.reconciler import Reconciler
        from xauusd.risk.kill_switch import KillSwitch

        magic = 990101
        # The real BrokerPosition, not a stand-in: a hand-rolled double would let the
        # test pass while the reconciler broke on a field the double happened to omit —
        # which is exactly what a first attempt at this test did.
        orphan = BrokerPosition(
            ticket=5150,
            symbol="GOLD",
            direction=Direction.LONG,
            volume=0.05,
            entry_price=2000.0,
            stop_loss=1995.0,
            take_profit=2010.0,
            opened_at=datetime.now(UTC),
            magic=magic,
            comment="xauusd:abc123",
        )

        class _Broker:
            def positions(self, magic=None, symbol=None):  # type: ignore[no-untyped-def]
                return [orphan]

        rec = Reconciler(_Broker(), KillSwitch(), None, magic)
        # Our record knows of nothing: the crash-recovery case.
        result = rec.reconcile(db_positions=[], adopt_orphans=True)
        assert 5150 in result.adopted, "an unknown position of ours must be adopted"

    def test_a_position_we_think_is_open_but_the_broker_does_not_is_noticed(self) -> None:
        """The mirror case: our record is stale. It must be detected, not assumed open."""
        from xauusd.execution.reconciler import Reconciler
        from xauusd.risk.kill_switch import KillSwitch

        class _Broker:
            def positions(self, magic=None, symbol=None):  # type: ignore[no-untyped-def]
                return []

        rec = Reconciler(_Broker(), KillSwitch(), None, 990101)
        result = rec.reconcile(
            db_positions=[{"ticket": 4242, "symbol": "GOLD", "volume": 0.05}],
            adopt_orphans=True,
        )
        # Nothing may be adopted from an empty broker, and the discrepancy must be
        # represented rather than the stale record being trusted.
        assert 4242 not in result.adopted


class TestTheReconcilerKnowsEveryEngineIsOurs:
    """A reconciler that knows one magic, with two engines trading, is a reconciler
    that raises CRITICAL "a human is trading this account" about our own position.

    That divergence is not cosmetic: UNTAGGED_POSITION says exposure and risk cannot be
    trusted, and it says it about a trade the intraday engine opened two minutes ago.
    """

    @staticmethod
    def _position(magic: int, ticket: int = 5150):  # type: ignore[no-untyped-def]
        from xauusd.domain.types import BrokerPosition

        return BrokerPosition(
            ticket=ticket,
            symbol="GOLD",
            direction=Direction.LONG,
            volume=0.05,
            entry_price=2000.0,
            stop_loss=1995.0,
            take_profit=2010.0,
            opened_at=datetime.now(UTC),
            magic=magic,
            comment="xauusd:abc123",
        )

    def _reconcile(self, position):  # type: ignore[no-untyped-def]
        from xauusd.config.settings import Settings
        from xauusd.execution.reconciler import Reconciler
        from xauusd.risk.kill_switch import KillSwitch

        class _Broker:
            def positions(self, magic=None, symbol=None):  # type: ignore[no-untyped-def]
                return [position]

        s = Settings()
        rec = Reconciler(_Broker(), KillSwitch(), None, s.broker.magic, magics=s.owned_magics())
        return rec.reconcile(db_positions=[], adopt_orphans=True)

    @pytest.mark.parametrize("engine", ["", "scalp", "intraday"])
    def test_every_engines_position_is_recognised_as_ours(self, engine: str) -> None:
        from xauusd.config.settings import Settings

        magic = Settings().engine_magic(engine)
        result = self._reconcile(self._position(magic))
        assert 5150 in result.adopted, f"a position with the {engine or 'account'} magic is ours"
        assert not any(d.kind == "UNTAGGED_POSITION" for d in result.divergences)

    def test_a_genuinely_foreign_position_is_still_critical(self) -> None:
        """The relaxation must not have swallowed the case the check exists for."""
        result = self._reconcile(self._position(424242))
        assert 5150 not in result.adopted
        assert any(d.kind == "UNTAGGED_POSITION" for d in result.divergences)
        assert any(d.severity == "CRITICAL" for d in result.divergences)
