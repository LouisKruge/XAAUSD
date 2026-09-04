"""Scalps are closed on scalp time, not A/A+ time.

`execution.time_stop_bars` is 48 M5 bars — four hours, right for a trade that needs
room to work. Applied to a scalp it turns a 90-minute setup into an accidental swing
trade held three times its design, and the entire premise of the short-duration engine
is return per unit of TIME.

`scalp.max_hold_minutes` existed as configuration that nothing read: the backtester and
the live position manager both consulted `time_stop_bars` for every position. One
definition now, keyed on the strategy that opened it, so they cannot disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xauusd.config.settings import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "xauusd"


class TestTheLimitDependsOnTheStrategy:
    def test_a_swing_trade_keeps_the_four_hour_stop(self) -> None:
        s = Settings()
        assert s.time_stop_bars_for("sweep_mss_fvg") == s.execution.time_stop_bars
        assert s.time_stop_bars_for("sweep_mss_fvg") * 5 == 240

    def test_a_scalp_is_closed_at_its_configured_hold(self) -> None:
        s = Settings()
        bars = s.time_stop_bars_for("scalp_sweep_reversal")
        assert bars * 5 == s.scalp.max_hold_minutes
        assert bars < s.execution.time_stop_bars, "a scalp must not outlive a swing"

    def test_every_scalp_model_is_recognised(self) -> None:
        s = Settings()
        for name in (
            "scalp_sweep_reversal",
            "scalp_fvg_retracement",
            "scalp_ob_reaction",
            "scalp_breakout_retest",
            "scalp_momentum_continuation",
        ):
            assert s.time_stop_bars_for(name) == 18

    def test_an_unknown_strategy_gets_the_conservative_limit(self) -> None:
        """Not knowing what opened a position must not shorten its leash arbitrarily."""
        s = Settings()
        assert s.time_stop_bars_for(None) == s.execution.time_stop_bars
        assert s.time_stop_bars_for("") == s.execution.time_stop_bars

    def test_the_limit_scales_with_the_decision_timeframe(self) -> None:
        """90 minutes is 18 M5 bars or 90 M1 bars; the minutes are what is configured."""
        s = Settings()
        assert s.time_stop_bars_for("scalp_x", bar_seconds=300) == 18
        assert s.time_stop_bars_for("scalp_x", bar_seconds=60) == 90

    def test_it_never_returns_zero_for_a_scalp(self) -> None:
        """Zero disables the time stop, which for a scalp means holding indefinitely."""
        s = Settings(scalp={"max_hold_minutes": 1})
        assert s.time_stop_bars_for("scalp_x", bar_seconds=3600) >= 1


class TestBothExecutionPathsUseIt:
    def test_no_path_reads_the_raw_time_stop(self) -> None:
        allowed = {"config/settings.py"}
        offenders = [
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if str(p.relative_to(SRC)).replace("\\", "/") not in allowed
            and re.search(r"e\.time_stop_bars|execution\.time_stop_bars", p.read_text())
        ]
        assert not offenders, (
            f"{offenders} read time_stop_bars directly. Use "
            f"settings.time_stop_bars_for(strategy) — a raw read holds every scalp for "
            f"four hours."
        )


class TestConcurrencyIsBoundedByTheRiskBudget:
    def test_the_configured_book_fits_the_global_cap(self) -> None:
        s = Settings()
        exposure = s.scalp.max_concurrent * s.scalp.risk_pct
        assert exposure <= s.risk.max_total_open_risk_pct

    def test_a_book_that_breaches_the_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="breaches the 2% global cap"):
            Settings(scalp={"max_concurrent": 20, "risk_pct": 0.0015})

    def test_concurrency_above_one_is_now_permitted(self) -> None:
        """The correlation gate is wired into ScalpPipeline, so the lock lifts — but
        the arithmetic still binds."""
        assert Settings(scalp={"max_concurrent": 5, "risk_pct": 0.0015}).scalp.max_concurrent == 5
