"""One definition of the reward-to-risk floor.

There were four checks. The A/A+ 1:2 floor was applied to scalps in the risk gate, in
the backtester's execution step, and in the live order manager's preflight — so a scalp
targeting 1.5R was approved by the risk gate and then silently refused at send time, in
both backtest and live. Each check was individually correct for the engine it was
written for, which is why none of them looked wrong.

`Settings.min_rr_for` is now the only definition. These tests pin it and assert no
caller has drifted back to reading `thresholds.min_rr` directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xauusd.config.settings import Settings
from xauusd.domain.enums import Classification

SRC = Path(__file__).resolve().parents[2] / "src" / "xauusd"


class TestTheFloorDependsOnTheTier:
    def test_a_plus_and_a_keep_the_two_to_one_floor(self) -> None:
        s = Settings()
        assert s.min_rr_for(Classification.A_PLUS) == 2.0
        assert s.min_rr_for(Classification.A) == 2.0

    def test_a_scalp_uses_its_own_gross_floor(self) -> None:
        s = Settings()
        assert s.min_rr_for(Classification.SCALP) == s.scalp.min_gross_rr
        assert s.min_rr_for(Classification.SCALP) < 2.0

    def test_an_unknown_classification_gets_the_strict_floor(self) -> None:
        """Degradation is one-directional: not knowing the tier must not relax it."""
        assert Settings().min_rr_for(None) == 2.0
        assert Settings().min_rr_for(Classification.NO_TRADE) == 2.0

    def test_the_scalp_floor_still_refuses_a_worthless_target(self) -> None:
        """Below 1.25 a high win rate stops being worth having, so the floor is a floor
        rather than an absence of one."""
        assert Settings().min_rr_for(Classification.SCALP) >= 1.25


class TestNoCallerReadsTheRawThreshold:
    """The bug was four independent reads of one setting. A fifth would reintroduce it."""

    ALLOWED = {
        "config/settings.py",  # the definition itself
        "strategy/gates.py",  # g_min_rr is an A/A+ PLAN_GATE; scalps never run it
        "cli.py",  # doctor prints the configured value
    }

    def test_only_the_allowed_files_read_thresholds_min_rr(self) -> None:
        offenders = []
        for path in SRC.rglob("*.py"):
            rel = str(path.relative_to(SRC))
            if rel.replace("\\", "/") in self.ALLOWED:
                continue
            if re.search(r"thresholds\.min_rr", path.read_text()):
                offenders.append(rel)
        assert not offenders, (
            f"{offenders} read thresholds.min_rr directly. Use settings.min_rr_for("
            f"classification) — a raw read applies the A/A+ 1:2 floor to every tier, "
            f"which silently refuses scalps after the risk gate has approved them."
        )

    def test_the_execution_paths_take_a_classification(self) -> None:
        """Both re-check RR at send time; both must know which floor applies."""
        preflight = (SRC / "execution" / "order_manager.py").read_text()
        assert "classification" in preflight.split("def preflight")[1][:400]

        backtest = (SRC / "backtesting" / "engine.py").read_text()
        assert "min_rr_for(decision.classification)" in backtest


class TestTheFloorIsActuallyApplied:
    @pytest.mark.parametrize(
        ("classification", "rr", "expected"),
        [
            (Classification.A, 1.5, False),
            (Classification.A, 2.0, True),
            (Classification.SCALP, 1.5, True),
            (Classification.SCALP, 1.0, False),
        ],
    )
    def test_the_right_trades_clear_the_right_floor(
        self, classification: Classification, rr: float, expected: bool
    ) -> None:
        assert (rr >= Settings().min_rr_for(classification)) is expected
