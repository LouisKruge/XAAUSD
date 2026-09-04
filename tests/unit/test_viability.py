"""Account viability against the broker's minimum lot.

The sizer already refuses to round a sub-minimum position up — it floors to the volume
step and rejects below volume_min rather than silently risking several percent on one
minimum lot. These tests pin the *account-level* statement of the same fact, because a
per-trade rejection arrives silently: on an account too small for the instrument every
setup is refused for one structural reason, and what the operator sees is a bot that
never trades, which looks exactly like a strategy being too selective.
"""

from __future__ import annotations

import pytest

from xauusd.domain.types import SymbolSpec
from xauusd.risk.viability import assess_account


def gold() -> SymbolSpec:
    """The broker's actual XAUUSD spec: 100oz contract, 0.01 minimum lot, $1/tick."""
    return SymbolSpec("XAUUSD", 2, 0.01, 100.0, 0.01, 1.0, 1.0, 1.0, 0.01, 50.0, 0.01, 10, 5)


class TestAMicroAccountIsRefusedLoudly:
    """R300 is roughly $16.50. One minimum lot at a structurally honest $2 stop risks
    $2.00 — 12% of the account, against a 0.15% budget."""

    def test_the_verdict_is_not_viable(self) -> None:
        r = assess_account(gold(), equity=16.48, risk_pct=0.0015, stop_distance=2.00)
        assert not r.viable
        assert r.loss_per_min_lot == pytest.approx(2.00)
        assert r.forced_risk_pct == pytest.approx(0.1213, abs=1e-4)
        assert r.concurrent_supported == 0

    def test_it_reports_the_shortfall_as_a_multiple(self) -> None:
        """'81x too small' is actionable. 'Trade rejected' eighty times is not."""
        r = assess_account(gold(), equity=16.48, risk_pct=0.0015, stop_distance=2.00)
        assert r.min_viable_equity == pytest.approx(1333.33, abs=0.01)
        assert round(r.shortfall_multiple) == 81

    def test_it_uses_the_mandated_wording(self) -> None:
        r = assess_account(gold(), equity=16.48, risk_pct=0.0015, stop_distance=2.00)
        text = " ".join(r.lines())
        assert "NOT EXECUTIONALLY VIABLE UNDER CURRENT BROKER CONDITIONS" in text

    def test_it_says_the_silence_is_the_sizer_working(self) -> None:
        """The whole point: distinguish 'too small' from 'too selective'."""
        text = " ".join(assess_account(gold(), 16.48, 0.0015, 2.00).lines())
        assert "not a strategy that is being too selective" in text


class TestTheStopIsNeverChosenToFitTheAccount:
    """Shrinking the stop until the arithmetic works is the failure this exists to
    surface. A stop small enough to fit R300 is inside the spread."""

    def test_a_stop_that_fits_a_micro_account_is_absurdly_small(self) -> None:
        spec = gold()
        equity, risk = 16.48, 0.0015
        # Largest stop whose minimum-lot loss fits the budget:
        fitting_stop = (equity * risk) / (spec.volume_min * spec.contract_size)
        assert fitting_stop < 0.03, "a fitting stop is under 3 cents"

        # The spread alone is 25 points = $0.25 — ten times wider than the stop that
        # would fit. Such a trade is underwater the instant it opens, so there is no
        # stop distance at which R300 both fits the risk budget and clears its costs.
        spread_price = 25 * spec.point
        assert fitting_stop * 10 < spread_price


class TestAViableAccountPasses:
    def test_a_larger_account_is_viable_and_reports_concurrency(self) -> None:
        # $1,400 at 0.15% budgets $2.10 — one minimum lot at a $2.00 stop fits.
        r = assess_account(gold(), equity=1400.0, risk_pct=0.0015, stop_distance=2.00)
        assert r.viable
        assert r.concurrent_supported == 1

    def test_concurrency_scales_with_equity(self) -> None:
        """The 10-concurrent design needs the budget to cover ten minimum lots."""
        r = assess_account(gold(), equity=14_000.0, risk_pct=0.0015, stop_distance=2.00)
        assert r.concurrent_supported == 10

    def test_a_wider_stop_needs_more_equity(self) -> None:
        tight = assess_account(gold(), 1400.0, 0.0015, 2.00)
        wide = assess_account(gold(), 1400.0, 0.0015, 5.00)
        assert wide.min_viable_equity > tight.min_viable_equity
        assert not wide.viable


class TestMargin:
    def test_margin_is_reported_when_leverage_is_known(self) -> None:
        r = assess_account(
            gold(), equity=16.48, risk_pct=0.0015, stop_distance=2.00,
            price=2600.0, leverage=500,
        )
        assert r.margin_per_min_lot == pytest.approx(5.20)
        # One position consumes a third of the account before any loss.
        assert r.margin_per_min_lot / r.equity > 0.30

    def test_margin_is_absent_without_leverage(self) -> None:
        assert assess_account(gold(), 1400.0, 0.0015, 2.00).margin_per_min_lot is None


class TestInputsAreValidated:
    @pytest.mark.parametrize("stop", [0.0, -1.0])
    def test_a_nonpositive_stop_is_refused(self, stop: float) -> None:
        with pytest.raises(ValueError, match="stop_distance"):
            assess_account(gold(), 1000.0, 0.0015, stop)

    def test_a_nonpositive_risk_is_refused(self) -> None:
        with pytest.raises(ValueError, match="risk_pct"):
            assess_account(gold(), 1000.0, 0.0, 2.00)
