"""A zero-trade backtest must say WHY, in its own numbers.

The report that prompted this: 39,992 harvested M1 bars, a full week of real gold, and
`0 trades` above a rejection ledger of A/A+ gate names. The reasonable reading was "the
data is wrong". It was not. The models produced hundreds of candidates and one
threshold — `min_score`, set to 65 by judgement — sat above the 90th percentile of the
scores those models can actually produce, so nothing ever cleared it.

Those two situations are the ones that most need telling apart, because the correct
response to each is the opposite of the response to the other:

    no candidates       -> a data or market question; the thresholds are irrelevant
    candidates, none    -> a threshold question; the data is fine

and until this method existed a run could not distinguish them at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd.backtesting.engine import BacktestResult
from xauusd.backtesting.metrics import Metrics


def _result(scores: list[float]) -> BacktestResult:
    return BacktestResult(
        trades=[],
        decisions=[],
        equity_curve=[],
        metrics=Metrics(),
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 8, tzinfo=UTC),
        cost_model={},
        config_hash="x",
        data_hash="y",
        bars_evaluated=1,
        wall_seconds=0.0,
        scalp_scores=scores,
    )


class TestTheTwoZeroTradeCasesAreDistinguishable:
    def test_no_candidates_says_so_and_points_away_from_thresholds(self) -> None:
        text = _result([]).scalp_diagnosis(65.0)
        assert "no candidates" in text.lower()
        assert "not a threshold" in text.lower()

    def test_candidates_but_none_cleared_names_the_threshold_as_the_constraint(self) -> None:
        """The exact situation that cost a week: 348 candidates, max score 74, min 65."""
        text = _result([50.0, 54.0, 58.0, 61.0, 64.0]).scalp_diagnosis(65.0)
        assert "NOTHING cleared it" in text
        assert "binding constraint is the threshold, not the market" in text
        # The best candidate's score has to appear, because "nothing cleared 65" and
        # "the best was 64" and "the best was 20" call for very different responses.
        assert "64" in text

    def test_it_refuses_to_recommend_simply_lowering_the_threshold(self) -> None:
        """A diagnosis that ends "so lower it" is how a backtest starts lying.

        The brief is explicit — do not lower thresholds blindly — and a message the
        operator reads at exactly the moment they are frustrated is the wrong place to
        be silent about that.
        """
        text = _result([10.0, 20.0]).scalp_diagnosis(65.0)
        assert "do not simply lower it" in text.lower()
        assert "sweep" in text.lower()


class TestItReportsWhereTheThresholdSits:
    def test_the_percentile_is_stated(self) -> None:
        text = _result([float(i) for i in range(100)]).scalp_diagnosis(90.0)
        assert "90th percentile" in text

    def test_a_threshold_below_everything_reports_the_zeroth_percentile(self) -> None:
        text = _result([70.0, 80.0, 90.0]).scalp_diagnosis(10.0)
        assert "0th percentile" in text
        assert "NOTHING cleared it" not in text

    def test_a_very_selective_threshold_is_flagged_as_unmeasurable(self) -> None:
        """Passing 1% of candidates is not selectivity, it is a sample size problem.

        Something cleared it, so the run does not read as broken — which is exactly when
        a too-small sample slips through as if it meant something.
        """
        text = _result([float(i) for i in range(100)]).scalp_diagnosis(99.0)
        assert "too small to measure an edge" in text

    def test_a_healthy_threshold_is_not_flagged(self) -> None:
        text = _result([float(i) for i in range(100)]).scalp_diagnosis(70.0)
        assert "too small to measure" not in text
        assert "NOTHING cleared it" not in text

    def test_the_counts_are_right(self) -> None:
        text = _result([float(i) for i in range(100)]).scalp_diagnosis(60.0)
        assert "100 candidates scored, 40 cleared" in text


class TestItSurvivesDegenerateInput:
    @pytest.mark.parametrize("scores", [[0.0], [100.0], [50.0] * 3, [0.0, 100.0]])
    def test_small_and_flat_distributions_do_not_raise(self, scores: list[float]) -> None:
        assert _result(scores).scalp_diagnosis(65.0)

    def test_the_top_percentile_index_cannot_run_off_the_end(self) -> None:
        """`int(n * 0.9)` reaches n for n = 1..9 at some quantile; a naive index raises
        IndexError only on short runs, which is where a diagnostic is needed most."""
        for n in range(1, 12):
            assert _result([float(i) for i in range(n)]).scalp_diagnosis(5.0)
