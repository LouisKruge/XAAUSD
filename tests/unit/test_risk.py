"""Risk engine. Every 'NEVER ALLOWED' behaviour from the brief has a test here
asserting it is impossible, not merely discouraged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from xauusd.config.settings import RiskConfig, Settings
from xauusd.domain.enums import Classification, Direction, KillSwitchReason, Timeframe
from xauusd.domain.types import (
    AccountState,
    BrokerPosition,
    SymbolSpec,
    TargetLevel,
    TradePlan,
)
from xauusd.risk.drawdown import DrawdownGuard, period_start
from xauusd.risk.gate import RiskGate
from xauusd.risk.kill_switch import KillSwitch
from xauusd.risk.position_sizing import (
    PositionSizer,
    RiskInvariantViolation,
    SizingInputs,
)

UTC = UTC
T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def spec(**kw) -> SymbolSpec:  # type: ignore[no-untyped-def]
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
        commission_per_lot=7.0,
    )
    base.update(kw)
    return SymbolSpec(**base)  # type: ignore[arg-type]


def account(equity: float = 10_000.0) -> AccountState:
    return AccountState(1, "USD", equity, equity, 0.0, equity, 0.0)


def plan(
    direction: Direction = Direction.LONG,
    entry: float = 2000.0,
    sl: float = 1990.0,
    tp: float = 2020.0,
) -> TradePlan:
    rr = abs(tp - entry) / abs(entry - sl)
    return TradePlan(
        "test",
        "1.0",
        direction,
        entry,
        sl,
        (TargetLevel(tp, rr, "PDH liquidity"),),
        T0,
        Timeframe.M15,
        "invalidation",
        symbol="XAUUSD",
    )


class TestSizingArithmetic:
    def test_canonical_case(self) -> None:
        """$10k, 1% risk, $10 gold stop. 1 lot loses $1000, so 0.1 lots risks $100."""
        r = PositionSizer().calculate(
            SizingInputs(10_000, 0.01, 2000.0, 1990.0, Direction.LONG, spec())
        )
        assert r.approved
        assert r.lots == pytest.approx(0.10)
        assert r.risk_money == pytest.approx(100.0)
        assert r.risk_pct == pytest.approx(0.01)

    def test_wider_stop_means_smaller_position(self) -> None:
        s = PositionSizer()
        tight = s.calculate(SizingInputs(10_000, 0.01, 2000, 1995, Direction.LONG, spec()))
        wide = s.calculate(SizingInputs(10_000, 0.01, 2000, 1980, Direction.LONG, spec()))
        assert tight.lots > wide.lots
        assert tight.risk_money == pytest.approx(wide.risk_money, rel=0.05)

    def test_non_usd_account_applies_the_fx_rate(self) -> None:
        s = PositionSizer()
        usd = s.calculate(SizingInputs(10_000, 0.01, 2000, 1990, Direction.LONG, spec()))
        zar = s.calculate(
            SizingInputs(
                10_000,
                0.01,
                2000,
                1990,
                Direction.LONG,
                spec(),
                account_currency="ZAR",
                fx_rate_to_account=18.5,
            )
        )
        assert zar.lots < usd.lots


class TestSizingRefusals:
    def test_account_too_small_refuses_rather_than_shrinking_the_stop(self) -> None:
        """Shrinking a structural stop to fit lot granularity destroys the edge."""
        r = PositionSizer().calculate(
            SizingInputs(200.0, 0.01, 2000.0, 1990.0, Direction.LONG, spec())
        )
        assert not r.approved
        assert "too small" in r.reason
        assert r.lots == 0.0

    def test_stop_inside_the_brokers_minimum_is_refused(self) -> None:
        r = PositionSizer().calculate(
            SizingInputs(10_000, 0.01, 2000.0, 1999.95, Direction.LONG, spec())
        )
        assert not r.approved and "minimum" in r.reason

    def test_broker_disagreement_refuses_the_trade(self) -> None:
        """We compute the risk AND ask MT5; a disagreement means we cannot verify the spec."""
        r = PositionSizer().calculate(
            SizingInputs(10_000, 0.01, 2000.0, 1990.0, Direction.LONG, spec()),
            broker_calc_profit=-1300.0,  # broker says 1 lot loses $1300, we say $1000
        )
        assert not r.approved
        assert "disagrees with the broker" in r.reason
        assert r.cross_check_delta == pytest.approx(0.2308, abs=1e-3)

    def test_broker_agreement_passes(self) -> None:
        r = PositionSizer().calculate(
            SizingInputs(10_000, 0.01, 2000.0, 1990.0, Direction.LONG, spec()),
            broker_calc_profit=-1000.0,
        )
        assert r.approved and r.cross_check_delta == pytest.approx(0.0)

    def test_insufficient_margin_refuses(self) -> None:
        r = PositionSizer().calculate(
            SizingInputs(10_000, 0.01, 2000.0, 1990.0, Direction.LONG, spec(), free_margin=1000.0),
            broker_calc_margin=900.0,
        )
        assert not r.approved and "margin" in r.reason

    def test_over_cap_request_raises_rather_than_clamping(self) -> None:
        """A sizing call above the cap is a BUG upstream; clamping would hide it."""
        with pytest.raises(RiskInvariantViolation, match="exceeds the global cap"):
            PositionSizer().calculate(
                SizingInputs(10_000, 0.05, 2000.0, 1990.0, Direction.LONG, spec())
            )


class TestSizingProperties:
    @hyp_settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        equity=st.floats(500, 5_000_000, allow_nan=False),
        risk_pct=st.floats(0.0005, 0.02, allow_nan=False),
        entry=st.floats(1200, 5000, allow_nan=False),
        stop_distance=st.floats(0.5, 120.0, allow_nan=False),
        step=st.sampled_from([0.01, 0.1, 1.0]),
        tick_value=st.floats(0.1, 10.0, allow_nan=False),
    )
    def test_risk_never_exceeds_the_cap(
        self,
        equity: float,
        risk_pct: float,
        entry: float,
        stop_distance: float,
        step: float,
        tick_value: float,
    ) -> None:
        """The core invariant, across every plausible broker specification."""
        s = spec(
            volume_step=step,
            volume_min=step,
            tick_value_loss=tick_value,
            tick_value=tick_value,
            tick_value_profit=tick_value,
        )
        r = PositionSizer().calculate(
            SizingInputs(equity, risk_pct, entry, entry - stop_distance, Direction.LONG, s)
        )
        if not r.approved:
            assert r.lots == 0.0
            return
        assert r.risk_money <= equity * risk_pct * 1.005 + 1e-6
        # and the lot size must be a valid broker volume
        assert r.lots >= s.volume_min
        assert r.lots <= s.volume_max
        assert abs(round(r.lots / step) - r.lots / step) < 1e-6


class TestDrawdown:
    def test_measured_from_the_peak_not_the_start(self) -> None:
        """Making 3% then giving it back is a drawdown, not break-even."""
        g = DrawdownGuard()
        g.update(T0, 10_000)
        g.update(T0 + timedelta(hours=1), 10_300)
        g.update(T0 + timedelta(hours=2), 10_000)
        day = g.periods["DAY"]
        assert day.drawdown_pct == pytest.approx(0.0291, abs=1e-3)
        assert day.drawdown_from_start_pct == 0.0

    def test_daily_breach_locks_out(self) -> None:
        g = DrawdownGuard()
        g.update(T0, 10_000)
        g.update(T0 + timedelta(hours=1), 9_790)
        assert g.periods["DAY"].locked
        assert g.any_locked and "DAY" in g.locked_periods()

    def test_remaining_budget_caps_position_size(self) -> None:
        """With 0.4% of the daily budget left, a 1% trade is sized to 0.4%, not 1%."""
        g = DrawdownGuard()
        g.update(T0, 10_000)
        g.update(T0 + timedelta(hours=1), 9_840)  # 1.6% down of a 2% limit
        assert g.remaining_budget_pct() == pytest.approx(0.004, abs=1e-4)

    def test_weekly_lockout_survives_a_day_roll(self) -> None:
        """A weekly breach deserves a human, so it must not clear itself overnight."""
        g = DrawdownGuard()
        g.update(T0, 10_000)
        g.update(T0 + timedelta(hours=1), 9_400)  # breaches weekly 5%
        assert g.periods["WEEK"].locked
        g.update(T0 + timedelta(days=1), 9_400)  # next day
        assert g.periods["WEEK"].locked, "a manual-clear lockout must survive the roll"

    def test_manual_clear(self) -> None:
        g = DrawdownGuard()
        g.update(T0, 10_000)
        g.update(T0 + timedelta(hours=1), 9_400)
        assert g.clear("WEEK", by="operator", now=T0 + timedelta(hours=2))
        assert not g.periods["WEEK"].locked

    def test_consecutive_losses_lock_out(self) -> None:
        g = DrawdownGuard()
        g.update(T0, 10_000)
        for _ in range(4):
            g.record_trade(T0, -1.0)
        assert g.periods["DAY"].locked
        assert "consecutive losses" in g.periods["DAY"].lock_reason

    def test_daily_period_anchors_to_broker_time(self) -> None:
        """Anchoring to UTC midnight instead would misalign with the broker's own day."""
        utc = period_start(T0, "DAY", broker_offset_seconds=0)
        broker = period_start(T0, "DAY", broker_offset_seconds=3 * 3600)
        assert utc != broker


class TestKillSwitch:
    def test_auto_clearable_conditions_recover(self) -> None:
        k = KillSwitch()
        k.evaluate(T0, broker_ok=False)
        assert k.active
        k.evaluate(T0, broker_ok=True)
        assert not k.active

    def test_manual_conditions_do_not_self_clear(self) -> None:
        k = KillSwitch()
        k.evaluate(T0, weekly_breached=True)
        assert k.is_active(KillSwitchReason.WEEKLY_DRAWDOWN)
        assert not k.clear(KillSwitchReason.WEEKLY_DRAWDOWN, by="auto")
        assert k.is_active(KillSwitchReason.WEEKLY_DRAWDOWN)
        assert k.clear(KillSwitchReason.WEEKLY_DRAWDOWN, by="operator", force=True)

    def test_staleness_is_ignored_when_the_market_is_closed(self) -> None:
        k = KillSwitch()
        k.evaluate(T0, quote_age_seconds=600, max_quote_age=10, market_open=False)
        assert not k.is_active(KillSwitchReason.STALE_DATA)

    def test_spec_change_trips(self) -> None:
        k = KillSwitch()
        k.evaluate(T0, spec_changed=True)
        assert k.is_active(KillSwitchReason.SPEC_CHANGED)

    def test_blocks_entry_reports_why(self) -> None:
        k = KillSwitch()
        k.trip(KillSwitchReason.MANUAL, "operator halt")
        blocked, why = k.blocks_entry()
        assert blocked and "operator halt" in why


class TestRiskGate:
    def _gate(self, **cfg) -> RiskGate:  # type: ignore[no-untyped-def]
        s = Settings(risk=RiskConfig(**cfg)) if cfg else Settings()
        g = RiskGate(s)
        g.drawdown.update(T0, 10_000.0)
        return g

    def test_approves_a_valid_a_trade(self) -> None:
        g = self._gate()
        d = g.evaluate(plan(), Classification.A, account(), spec(), T0)
        assert d.approved and d.sizing.lots == pytest.approx(0.10)
        assert d.sizing.risk_pct <= 0.01 + 1e-9

    def test_a_plus_may_risk_more_but_never_automatically(self) -> None:
        g = self._gate()
        a = g.evaluate(plan(), Classification.A, account(), spec(), T0)
        aplus = g.evaluate(plan(), Classification.A_PLUS, account(), spec(), T0)
        assert aplus.sizing.lots > a.sizing.lots
        assert aplus.sizing.risk_pct <= 0.02 + 1e-9

    def test_global_cap_overrides_the_class_cap(self) -> None:
        """Stage 6 sets a hard cap far below the class caps; it must bind."""
        g = self._gate(global_risk_cap_pct=0.0025)
        d = g.evaluate(plan(), Classification.A_PLUS, account(), spec(), T0)
        assert d.approved and d.sizing.risk_pct <= 0.0025 + 1e-9

    def test_drawdown_budget_binds(self) -> None:
        g = self._gate()
        g.drawdown.update(T0 + timedelta(hours=1), 9_840)  # 1.6% down, 0.4% left
        d = g.evaluate(plan(), Classification.A, account(9_840), spec(), T0)
        assert d.approved
        assert d.sizing.risk_pct <= 0.004 + 1e-9

    def test_no_stacking_on_the_same_symbol(self) -> None:
        """Averaging in and hedging are both prevented here as well as being absent
        from the Broker interface."""
        g = self._gate()
        existing = BrokerPosition(1, "XAUUSD", Direction.LONG, 0.1, 2000, 1990, 2020, T0)
        d = g.evaluate(plan(), Classification.A, account(), spec(), T0, open_positions=[existing])
        assert not d.approved
        assert "no_stacking" in " ".join(d.failed) or "concurrent" in " ".join(d.failed)

    def test_kill_switch_blocks(self) -> None:
        g = self._gate()
        g.kill_switch.trip(KillSwitchReason.MANUAL, "halt")
        d = g.evaluate(plan(), Classification.A, account(), spec(), T0)
        assert not d.approved and "risk.kill_switch" in d.failed

    def test_no_trade_classification_is_rejected(self) -> None:
        g = self._gate()
        d = g.evaluate(plan(), Classification.NO_TRADE, account(), spec(), T0)
        assert not d.approved

    def test_rr_floor_enforced_at_the_gate_too(self) -> None:
        g = self._gate()
        weak = plan(tp=2010.0)  # 1:1
        d = g.evaluate(weak, Classification.A, account(), spec(), T0)
        assert not d.approved and "risk.min_rr" in d.failed

    def test_trade_frequency_cap(self) -> None:
        g = self._gate()
        d = g.evaluate(plan(), Classification.A, account(), spec(), T0, trades_today=3)
        assert not d.approved and "risk.trades_per_day" in d.failed

    def test_binding_constraint_is_reported(self) -> None:
        """The journal must show WHICH cap bound, not just the resulting size."""
        g = self._gate(global_risk_cap_pct=0.0025)
        d = g.evaluate(plan(), Classification.A_PLUS, account(), spec(), T0)
        detail = " ".join(c.detail for c in d.checks if c.name == "risk.budget_available")
        assert "global_cap" in detail
