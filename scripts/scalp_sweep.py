#!/usr/bin/env python3
"""Sweep the scalp engine's two most consequential unknowns, on REAL history.

`min_score` and `target_rr` were set by judgement, not measurement — 65 and 1.5 are
guesses, and a guess that decides whether to risk money is exactly what this project
has spent longest learning not to trust. This replaces both with numbers.

It reports NET figures after the spread, slippage and commission actually paid, and it
reports the trade count beside them, because a configuration earning +0.4R on three
trades has told you nothing.

**It cannot tell you the strategy is profitable.** It can tell you which configuration
was least bad on the history you have, which is a weaker and different claim. An
in-sample sweep is where overfitting comes from: treat the winner as a hypothesis to
test out-of-sample, never as a setting to deploy.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauusd.backtesting.engine import BacktestConfig, BacktestEngine
from xauusd.config.settings import load_settings
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import SymbolSpec
from xauusd.monitoring.logging import configure_logging

MODELS = [
    "scalp_sweep_reversal",
    "scalp_fvg_retracement",
    "scalp_ob_reaction",
    "scalp_breakout_retest",
    "scalp_momentum_continuation",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="sweep scalp score threshold and target RR")
    ap.add_argument("--scores", default="50,55,60,65,70")
    ap.add_argument("--rrs", default="1.25,1.5,1.75,2.0")
    ap.add_argument("--warmup", type=int, default=2000)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--source", default="mt5")
    ap.add_argument(
        "--min-trades",
        type=int,
        default=30,
        help="configurations with fewer trades are shown but never ranked",
    )
    args = ap.parse_args()

    # `logging.disable(logging.CRITICAL)` was here and did NOTHING. structlog is only
    # pointed at stdlib inside `configure_logging`; a script that never calls it gets
    # structlog's default PrintLogger, which writes straight to stdout and never
    # consults stdlib levels. So every decision instant emitted several DEBUG lines —
    # millions of them across a full grid. That is not just noise: it buried the result
    # rows this script prints as it goes, so a running sweep looked like a hung one, and
    # the I/O itself is a measurable share of the runtime.
    configure_logging("ERROR", json_output=False)
    settings = load_settings()

    # Reuse the CLI loader so the sweep reads history exactly as a backtest does,
    # including reading it under the symbol it was actually stored with.
    from xauusd.cli import _load_data

    class _Args:
        synthetic = 0
        seed = 5
        source = args.source

    data = _load_data(_Args(), settings)
    m1 = data.get(Timeframe.M1)
    n_m1 = len(m1) if m1 else 0
    print(f"history          : {len(data[Timeframe.M5])} M5, {n_m1} M1")
    if n_m1 < 10_000:
        print(
            "\nWARNING: the scalp engine triggers on M1. With this little M1 history\n"
            "these results describe almost nothing. Harvest more M1 first."
        )

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

    scores = [float(x) for x in args.scores.split(",")]
    rrs = [float(x) for x in args.rrs.split(",")]
    configs = list(itertools.product(scores, rrs))

    # Every configuration re-walks the whole history, so the grid multiplies directly
    # into wall-clock time. Saying so up front is the difference between "this is taking
    # hours" and "this has hung" — and only one of those is worth killing.
    base_tf = Timeframe.M1 if n_m1 >= 20_000 else Timeframe.M5
    instants = max(0, (len(data[base_tf]) - args.warmup)) // max(args.step, 1)
    print(
        f"\nplan             : {len(configs)} configurations x ~{instants:,} decision "
        f"instants each\n"
        f"                   at roughly 10-15 instants/second this is on the order of "
        f"{len(configs) * instants / 12 / 3600:.1f} hours.\n"
        f"                   Narrow it with --scores/--rrs, or sample less densely with "
        f"a larger --step.\n"
        f"                   Rows print as each configuration finishes; nothing is lost "
        f"if you stop early."
    )

    print(
        f"\n{'score':>6} {'RR':>5} {'trades':>7} {'win%':>6} {'expR':>8} "
        f"{'PF':>6} {'maxDD':>7} {'totalR':>8}  progress"
    )
    rows = []
    candidates: list[tuple[float, float, int, float]] = []
    started = time.monotonic()
    for done, (score, rr) in enumerate(configs, start=1):
        tuned = settings.model_copy(
            update={
                "scalp": settings.scalp.model_copy(
                    update={
                        "enabled": True,
                        "enabled_models": MODELS,
                        "min_score": score,
                        "target_rr": rr,
                    }
                )
            }
        )
        engine = BacktestEngine(
            tuned,
            spec,
            BacktestConfig(
                starting_equity=10_000.0,
                warmup_bars=args.warmup,
                step=args.step,
                decision_timeframe=Timeframe.M5,
            ),
        )
        result = engine.run(data)
        m = result.metrics
        scalps = [t for t in result.trades if str(getattr(t, "strategy", "")).startswith("scalp")]
        candidates.append((score, rr, len(result.scalp_scores), max(result.scalp_scores or [0.0])))
        elapsed = time.monotonic() - started
        remaining = elapsed / done * (len(configs) - done)
        print(
            f"{score:>6.0f} {rr:>5.2f} {len(scalps):>7} {m.win_rate * 100:>5.1f}% "
            f"{m.expectancy_r:>+8.3f} {m.profit_factor:>6.2f} "
            f"{m.max_drawdown_pct * 100:>6.2f}% {m.total_r:>+8.2f}"
            f"  {done}/{len(configs)}, ~{remaining / 60:.0f} min left",
            flush=True,
        )
        rows.append((score, rr, len(scalps), m.win_rate, m.expectancy_r, m.total_r))

    # Say how many candidates existed before any threshold judged them. Without this,
    # "0 trades at every setting" reads as "the models found nothing", when the far more
    # likely reading is that a gate downstream of the score refused all of them — and
    # those call for opposite responses.
    if candidates:
        best_cand = max(c[2] for c in candidates)
        best_seen = max(c[3] for c in candidates)
        print(
            f"\ncandidates detected  : up to {best_cand} per configuration, "
            f"best score seen {best_seen:.0f}"
        )

    ranked = [r for r in rows if r[2] >= args.min_trades]
    print()
    if not ranked:
        best_n = max((r[2] for r in rows), default=0)
        print(
            f"NO CONFIGURATION produced {args.min_trades} trades (best was {best_n}).\n"
            f"Nothing here can be ranked: an expectancy computed on a handful of trades\n"
            f"is noise, and picking the best of them is how a backtest lies. Either the\n"
            f"history is too short, or these setups do not occur often enough on it to\n"
            f"support a short-duration engine."
        )
        return 1

    best = max(ranked, key=lambda r: r[4])
    print(
        f"Best NET expectancy with >= {args.min_trades} trades:\n"
        f"  min_score {best[0]:.0f}, target_rr {best[1]:.2f} -> {best[2]} trades, "
        f"win {best[3] * 100:.1f}%, expectancy {best[4]:+.3f}R, total {best[5]:+.2f}R"
    )
    if best[4] <= 0:
        print(
            "\nThat is NOT deployable: expectancy is not positive after costs. The\n"
            "honest conclusion is that no setting in this grid has an edge on this\n"
            "history."
        )
        return 1
    print(
        "\nThis is an IN-SAMPLE result and is not evidence of an edge. It is the\n"
        "hypothesis to test out-of-sample and walk-forward before anything is deployed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
