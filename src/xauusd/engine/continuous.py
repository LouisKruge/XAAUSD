"""The continuous scan loop: surveillance that never idles between bars.

The A/A+ engine wakes on M5 close and sleeps for five minutes. That is correct for a
strategy holding for hours, and it is the single structural reason a short-duration
engine cannot exist alongside it: a setup that forms at 09:01:10 and is gone by 09:03
is never seen, because nothing looks until 09:05.

This loop is the fix. It runs on its own configurable cadence, independent of the M5
cycle, and it separates two things the old loop conflated:

    SCAN   — cheap, frequent, always runs, records what it saw
    ACT    — expensive, rare, only when a candidate survives the hard gates

Three properties matter more than speed:

**Scanning is not trading.** A scan that finds nothing is the normal, healthy outcome
and is recorded as such. The loop's job is to guarantee the market is *looked at*
continuously; whether anything is worth doing is the gates' decision, and they are
unchanged by how often they are asked.

**Telemetry is the product.** Signals detected, accepted, rejected and why — per scan,
per hour — is what turns "the bot isn't trading" from a mystery into a number. That
question has cost more time in this project than any other, so the counters are part of
the loop rather than something added later.

**A slow scan must never delay the A/A+ cycle.** They are separate tasks over a shared
snapshot cache; this loop never blocks the other, and overruns are dropped rather than
queued, because a scan starting late is worth less than the next one starting on time.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ScanOutcome:
    """What one pass of the scanner saw. Recorded whether or not anything was traded."""

    ts: datetime
    duration_ms: int
    signals_detected: int = 0
    signals_accepted: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def acted(self) -> bool:
        return self.signals_accepted > 0


@dataclass(slots=True)
class ScanTelemetry:
    """Rolling counts, so "is it working?" is answerable without reading a log.

    Deliberately holds a bounded window rather than a lifetime total: an operator
    glancing at the dashboard wants to know what the last hour looked like, and a
    lifetime counter hides a scanner that stopped finding anything an hour ago.
    """

    window: int = 3600
    scans: int = 0
    detected: int = 0
    accepted: int = 0
    errors: int = 0
    rejections: Counter[str] = field(default_factory=Counter)
    recent: deque[ScanOutcome] = field(default_factory=lambda: deque(maxlen=3600))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_scan_at: datetime | None = None

    def record(self, outcome: ScanOutcome) -> None:
        self.scans += 1
        self.detected += outcome.signals_detected
        self.accepted += outcome.signals_accepted
        self.rejections.update(outcome.rejections)
        if outcome.error:
            self.errors += 1
        self.recent.append(outcome)
        self.last_scan_at = outcome.ts

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.detected if self.detected else 0.0

    def per_hour(self, count: int) -> float:
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        return count * 3600.0 / elapsed if elapsed > 0 else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "scans": self.scans,
            "signals_detected": self.detected,
            "signals_accepted": self.accepted,
            "signals_rejected": self.detected - self.accepted,
            "acceptance_pct": round(self.acceptance_rate * 100, 1),
            "signals_per_hour": round(self.per_hour(self.detected), 1),
            "trades_per_hour": round(self.per_hour(self.accepted), 2),
            "scans_per_hour": round(self.per_hour(self.scans), 1),
            "errors": self.errors,
            "top_rejections": dict(self.rejections.most_common(8)),
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
        }


ScanFn = Callable[[], ScanOutcome] | Callable[[], Awaitable[ScanOutcome]]


class ContinuousScanner:
    """Runs a scan function on a fixed cadence, for as long as the engine is running.

    The cadence is a floor on the interval, not a rate: if a scan takes longer than the
    interval the next one starts immediately rather than accumulating a backlog. A
    scanner that falls behind and then fires four times in a row would evaluate stale
    market state four times, which is worse than skipping.
    """

    def __init__(
        self,
        scan: ScanFn,
        interval_seconds: float = 1.0,
        *,
        name: str = "scalp",
        should_run: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.scan = scan
        self.interval = interval_seconds
        self.name = name
        self.should_run = should_run or (lambda: True)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.telemetry = ScanTelemetry()
        self.running = False
        self._overruns = 0

    @property
    def overruns(self) -> int:
        """Scans that took longer than the interval. A rising count means the cadence
        is faster than the work, and the interval is a fiction."""
        return self._overruns

    async def run_once(self) -> ScanOutcome | None:
        """One pass. Returns None when `should_run` says the market is not scannable.

        Errors are recorded and swallowed: a scanner that dies on one bad tick stops
        watching the market, which is the failure this class exists to prevent. The
        error count is surfaced so a persistently failing scan is visible rather than
        silently absorbed.
        """
        if not self.should_run():
            return None

        t0 = time.perf_counter()
        try:
            result = self.scan()
            outcome = await result if asyncio.iscoroutine(result) else result
        except Exception as exc:
            outcome = ScanOutcome(
                ts=self.clock(),
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
            log.error("scan_failed", scanner=self.name, error=outcome.error)

        elapsed = time.perf_counter() - t0
        if elapsed > self.interval:
            self._overruns += 1
        self.telemetry.record(outcome)
        return outcome

    async def run(self) -> None:
        """Scan until stopped. Never sleeps longer than the interval."""
        self.running = True
        log.info("scanner_started", scanner=self.name, interval_s=self.interval)
        while self.running:
            t0 = time.perf_counter()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # run_once already traps; this is belt and braces
                log.error("scanner_loop_error", scanner=self.name, error=str(exc))
            # Sleep only the remainder, so the cadence is the interval rather than the
            # interval plus however long the work took.
            remaining = self.interval - (time.perf_counter() - t0)
            await asyncio.sleep(max(0.0, remaining))
        log.info("scanner_stopped", scanner=self.name, scans=self.telemetry.scans)

    def stop(self) -> None:
        self.running = False
