"""The continuous scan loop.

The A/A+ engine wakes on M5 close and sleeps five minutes. That is the structural
reason a short-duration engine could not exist: a setup forming at 09:01 and gone by
09:03 is never seen. These tests pin the properties that make continuous surveillance
trustworthy — it keeps scanning through failures, it does not drift, and it records
what it saw whether or not it traded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from xauusd.engine.continuous import ContinuousScanner, ScanOutcome, ScanTelemetry


def outcome(detected: int = 0, accepted: int = 0, **rej: int) -> ScanOutcome:
    return ScanOutcome(
        ts=datetime.now(UTC),
        duration_ms=1,
        signals_detected=detected,
        signals_accepted=accepted,
        rejections=dict(rej),
    )


class TestItKeepsScanning:
    @pytest.mark.asyncio
    async def test_a_failing_scan_does_not_stop_the_loop(self) -> None:
        """A scanner that dies on one bad tick stops watching the market — which is
        precisely the failure this class exists to prevent."""
        calls = {"n": 0}

        def boom() -> ScanOutcome:
            calls["n"] += 1
            raise RuntimeError("bad tick")

        s = ContinuousScanner(boom, interval_seconds=0.01)
        for _ in range(3):
            await s.run_once()
        assert calls["n"] == 3
        assert s.telemetry.errors == 3
        assert s.telemetry.scans == 3, "a failed scan is still a scan, and is recorded"

    @pytest.mark.asyncio
    async def test_an_error_is_recorded_not_swallowed(self) -> None:
        s = ContinuousScanner(lambda: 1 / 0, interval_seconds=0.01)
        result = await s.run_once()
        assert result is not None and result.error is not None
        assert "ZeroDivisionError" in result.error

    @pytest.mark.asyncio
    async def test_it_runs_repeatedly_until_stopped(self) -> None:
        s = ContinuousScanner(lambda: outcome(detected=1), interval_seconds=0.01)
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.08)
        s.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert s.telemetry.scans >= 3, "should have scanned several times in 80ms"


class TestItDoesNotDrift:
    @pytest.mark.asyncio
    async def test_a_slow_scan_does_not_accumulate_a_backlog(self) -> None:
        """Firing four times in a row to catch up would evaluate stale market state
        four times, which is worse than skipping."""

        def slow() -> ScanOutcome:
            import time as _t

            _t.sleep(0.03)
            return outcome(detected=1)

        s = ContinuousScanner(slow, interval_seconds=0.01)
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.12)
        s.stop()
        await asyncio.wait_for(task, timeout=1.0)
        # ~30ms of work per scan in ~120ms: four-ish, not twelve.
        assert s.telemetry.scans <= 6, f"backlog accumulated: {s.telemetry.scans} scans"
        assert s.overruns > 0, "a scan slower than the interval must be counted"

    @pytest.mark.asyncio
    async def test_a_rejected_cadence_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="interval_seconds"):
            ContinuousScanner(lambda: outcome(), interval_seconds=0)


class TestScanningIsNotTrading:
    @pytest.mark.asyncio
    async def test_a_scan_that_finds_nothing_is_still_recorded(self) -> None:
        """The normal, healthy outcome. If empty scans were not recorded, a stopped
        scanner and an idle market would look identical."""
        s = ContinuousScanner(lambda: outcome(), interval_seconds=0.01)
        await s.run_once()
        assert s.telemetry.scans == 1
        assert s.telemetry.detected == 0
        assert s.telemetry.last_scan_at is not None

    @pytest.mark.asyncio
    async def test_should_run_suppresses_the_scan_entirely(self) -> None:
        """Market closed: not an error, not an empty scan — no scan."""
        s = ContinuousScanner(
            lambda: outcome(detected=5), interval_seconds=0.01, should_run=lambda: False
        )
        assert await s.run_once() is None
        assert s.telemetry.scans == 0


class TestTelemetryAnswersWhyItIsNotTrading:
    """The question that has cost more time in this project than any other."""

    def test_rejection_reasons_accumulate(self) -> None:
        t = ScanTelemetry()
        t.record(outcome(detected=3, accepted=1, scalp_cost_ratio=2))
        t.record(outcome(detected=2, accepted=0, scalp_cost_ratio=1, spread=1))
        assert t.detected == 5
        assert t.accepted == 1
        assert t.rejections["scalp_cost_ratio"] == 3
        assert t.rejections["spread"] == 1

    def test_acceptance_rate_is_reported(self) -> None:
        t = ScanTelemetry()
        t.record(outcome(detected=10, accepted=2))
        assert t.acceptance_rate == pytest.approx(0.2)
        assert t.as_dict()["acceptance_pct"] == 20.0

    def test_acceptance_rate_is_zero_not_an_error_with_no_signals(self) -> None:
        assert ScanTelemetry().acceptance_rate == 0.0

    def test_the_summary_names_the_top_rejections(self) -> None:
        t = ScanTelemetry()
        t.record(outcome(detected=9, accepted=0, spread=5, scalp_net_expectancy=4))
        top = t.as_dict()["top_rejections"]
        assert top == {"spread": 5, "scalp_net_expectancy": 4}

    def test_signals_rejected_is_derived_consistently(self) -> None:
        t = ScanTelemetry()
        t.record(outcome(detected=7, accepted=3))
        d = t.as_dict()
        assert d["signals_rejected"] == 4
        assert d["signals_detected"] == 7
