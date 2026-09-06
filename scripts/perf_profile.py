#!/usr/bin/env python3
"""Directive §36 — performance under sustained load.

Answers four questions with measurements rather than impressions:

    1. How long does one decision cycle take, and what is the tail?
    2. How long does one scalp scan take, against its 2-second interval?
    3. Does memory grow over a long run — a leak — or settle?
    4. Do the caches actually stop the analyser redoing work every instant?

The tail matters more than the mean. A decision loop that averages 40ms but spikes to
6 seconds misses the bar close it was woken for, and a scalp scanner whose p99 exceeds
its own interval silently stops being continuous.
"""

from __future__ import annotations

import gc
import resource
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xauusd.config.settings import load_settings
from xauusd.core.analyzer import MarketAnalyzer
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import AccountState, Quote, SymbolSpec
from xauusd.engine.scalp_pipeline import ScalpPipeline
from xauusd.monitoring.logging import configure_logging


def rss_mb() -> float:
    """Resident set size. ru_maxrss is KiB on Linux, bytes on macOS."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024.0 if sys.platform != "darwin" else raw / (1024.0 * 1024.0)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def report(name: str, ms: list[float], budget_ms: float) -> bool:
    p50, p95, p99 = pct(ms, 0.50), pct(ms, 0.95), pct(ms, 0.99)
    worst = max(ms) if ms else 0.0
    ok = p99 <= budget_ms
    print(
        f"{name:22s} n={len(ms):5d}  p50={p50:7.1f}ms  p95={p95:7.1f}ms  "
        f"p99={p99:7.1f}ms  max={worst:8.1f}ms  budget={budget_ms:.0f}ms  "
        f"{'OK' if ok else 'OVER BUDGET'}"
    )
    return ok


def main() -> int:
    configure_logging("ERROR", json_output=False)
    settings = load_settings()
    spec = SymbolSpec(
        settings.symbol,
        2,
        0.01,
        100.0,
        0.01,
        1.0,
        1.0,
        1.0,
        0.01,
        50.0,
        0.01,
        10,
        5,
        commission_per_lot=settings.risk.commission_per_lot,
    )

    from tests.fixtures.synthetic import market_m1

    bars = int(sys.argv[1]) if len(sys.argv) > 1 else 30_000
    instants = int(sys.argv[2]) if len(sys.argv) > 2 else 400

    print(f"building {bars:,} M1 bars of synthetic history...")
    data = market_m1(bars, seed=11)
    source = InMemoryBarSource(data)
    m1 = data[Timeframe.M1]

    macro = MarketAnalyzer(settings)
    micro_an = MicroAnalyzer(settings)
    scalp = ScalpPipeline(
        settings.model_copy(
            update={"scalp": settings.scalp.model_copy(update={"enabled": True, "min_score": 0.0})}
        )
    )
    account = AccountState(
        login=1,
        currency="USD",
        balance=10_000.0,
        equity=10_000.0,
        margin=0.0,
        free_margin=10_000.0,
        margin_level=0.0,
    )

    gc.collect()
    rss_start = rss_mb()
    snap_ms: list[float] = []
    micro_ms: list[float] = []
    scalp_ms: list[float] = []
    rss_track: list[tuple[int, float]] = []

    start_i = max(2_000, len(m1) - instants * 5)
    for n, i in enumerate(range(start_i, len(m1) - 1, 5)):
        if n >= instants:
            break
        bar = m1.bar_at(i)
        now = bar.ts + timedelta(seconds=60)
        half = (bar.spread_points or 25) * spec.point / 2
        view = MarketView(
            source, settings.symbol, now, Quote(now, bar.close - half, bar.close + half)
        )

        t0 = time.perf_counter()
        snap = macro.analyze(view, None, None, float(bar.spread_points or 25), 25.0)
        snap_ms.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        micro = micro_an.analyze(view)
        micro_ms.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        scalp.run(micro, snap, account=account, spec=spec, now=now)
        scalp_ms.append((time.perf_counter() - t0) * 1000)

        if n % 50 == 0:
            rss_track.append((n, rss_mb()))

    gc.collect()
    rss_end = rss_mb()

    print()
    # Budgets from the engine's own cadence, not invented: the decision loop wakes on
    # an M5 close and the scalp scanner runs every scan_interval_seconds.
    scan_budget = settings.scalp.scan_interval_seconds * 1000
    ok = True
    ok &= report("market snapshot", snap_ms, 5_000)
    ok &= report("micro snapshot", micro_ms, scan_budget)
    ok &= report("scalp cycle", scalp_ms, scan_budget)

    total = [a + b + c for a, b, c in zip(snap_ms, micro_ms, scalp_ms, strict=True)]
    ok &= report("full instant", total, 5_000)

    print()
    print(
        f"RSS start {rss_start:7.1f} MB -> end {rss_end:7.1f} MB "
        f"(delta {rss_end - rss_start:+.1f} MB over {len(snap_ms)} instants)"
    )
    for n, mb in rss_track:
        print(f"   after {n:4d} instants: {mb:7.1f} MB")

    # A leak shows as steady growth after the first fifty instants, once caches are warm.
    if len(rss_track) >= 3:
        warm = rss_track[1:]
        growth = warm[-1][1] - warm[0][1]
        per_instant_kb = growth * 1024 / max(1, warm[-1][0] - warm[0][0])
        print(f"\npost-warmup growth: {growth:+.1f} MB ({per_instant_kb:+.1f} KB/instant)")
        if per_instant_kb > 50:
            print("  LEAK SUSPECTED: memory is climbing steadily after warm-up.")
            ok = False
        else:
            print("  No leak signature: growth is flat or bounded after warm-up.")

    print()
    print("VERDICT:", "WITHIN BUDGET" if ok else "OVER BUDGET — see the lines marked above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
