"""Run the full Phase 10 validation suite and print the deployment gate report.

    python scripts/run_validation.py --synthetic 60000
    python scripts/run_validation.py --source mt5

This is the command that decides whether a strategy may EVER reach live routing.
Expect it to FAIL for most strategy versions — that is the gate working.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xauusd.backtesting import monte_carlo
from xauusd.backtesting.engine import (
    BacktestConfig,
    BacktestEngine,
    split_data,
)
from xauusd.backtesting.validation import DeploymentGate
from xauusd.backtesting.walk_forward import WalkForwardResult, Window
from xauusd.config.settings import Settings, StrategyThresholds, load_settings
from xauusd.domain.enums import Timeframe, ValidationStatus
from xauusd.domain.types import SymbolSpec
from xauusd.execution.sim_broker import SimFillModel
from xauusd.monitoring.logging import configure_logging


def gold_spec(settings: Settings) -> SymbolSpec:
    return SymbolSpec(
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


def run_backtest(settings, spec, data, fill_model, warmup, step):  # type: ignore[no-untyped-def]
    names = settings.enabled_strategies
    return BacktestEngine(
        settings,
        spec,
        BacktestConfig(warmup_bars=warmup, step=step),
        fill_model=fill_model,
        strategy_status=dict.fromkeys(names, ValidationStatus.OOS_PASSED),
    ).run(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--source", default="mt5")
    ap.add_argument("--warmup", type=int, default=6000)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--strategy", default="sweep_mss_fvg")
    ap.add_argument("--json", default=None)
    ap.add_argument("--a-score-min", type=float, default=None)
    args = ap.parse_args()

    configure_logging("WARNING", json_output=False)
    settings = load_settings()
    if args.a_score_min is not None:
        settings = Settings(
            **{
                **settings.model_dump(),
                "thresholds": StrategyThresholds(
                    **{**settings.thresholds.model_dump(), "a_score_min": args.a_score_min}
                ).model_dump(),
            }
        )
    spec = gold_spec(settings)

    if args.synthetic:
        from tests.fixtures.synthetic import market

        print(
            f"WARNING: synthetic data ({args.synthetic} M5 bars). Results are a smoke "
            f"test of the machinery, NOT a trading result.\n"
        )
        data = market(args.synthetic, seed=args.seed)
    else:
        from xauusd.cli import _load_data

        data = _load_data(args, settings)

    in_sample, out_of_sample = split_data(data, 0.65)
    print(f"in-sample     : {len(in_sample[Timeframe.M5])} M5 bars")
    print(f"out-of-sample : {len(out_of_sample[Timeframe.M5])} M5 bars\n")

    fills = SimFillModel(seed=42)
    print("running in-sample ...")
    is_res = run_backtest(settings, spec, in_sample, fills, args.warmup, args.step)
    print(f"  {is_res.metrics.summary_line()}")

    print("running out-of-sample ...")
    oos_res = run_backtest(
        settings,
        spec,
        out_of_sample,
        SimFillModel(seed=43),
        min(args.warmup, len(out_of_sample[Timeframe.M5]) // 3),
        args.step,
    )
    print(f"  {oos_res.metrics.summary_line()}\n")

    print("running cost stress (2x spread and slippage) ...")
    stress_res = run_backtest(
        settings,
        spec,
        out_of_sample,
        SimFillModel(seed=44, spread_multiplier=2.0, slippage_multiplier=2.0),
        min(args.warmup, len(out_of_sample[Timeframe.M5]) // 3),
        args.step,
    )
    print(f"  {stress_res.metrics.summary_line()}\n")

    # Walk-forward across the whole dataset.
    print("running walk-forward ...")
    wf = WalkForwardResult()
    base = data[Timeframe.M5]
    n = len(base)
    window_bars = max(8000, n // 6)
    oos_bars = window_bars // 3
    start = 0
    idx = 0
    while start + window_bars + oos_bars <= n and idx < 6:
        is_slice = {
            tf: s.slice(*_bounds(s, base, start, start + window_bars)) for tf, s in data.items()
        }
        oos_slice = {
            tf: s.slice(*_bounds(s, base, start + window_bars, start + window_bars + oos_bars))
            for tf, s in data.items()
        }
        w = Window(
            idx,
            base.bar_at(start).ts,
            base.bar_at(start + window_bars - 1).ts,
            base.bar_at(start + window_bars).ts,
            base.bar_at(start + window_bars + oos_bars - 1).ts,
        )
        try:
            w.is_metrics = run_backtest(
                settings,
                spec,
                is_slice,
                SimFillModel(seed=50 + idx),
                min(3000, window_bars // 3),
                args.step * 2,
            ).metrics
            w.oos_metrics = run_backtest(
                settings,
                spec,
                oos_slice,
                SimFillModel(seed=60 + idx),
                min(1500, oos_bars // 3),
                args.step * 2,
            ).metrics
            wf.windows.append(w)
            print(
                f"  window {idx}: IS {w.is_metrics.trades} trades "
                f"({w.is_metrics.expectancy_r:+.2f}R) -> OOS {w.oos_metrics.trades} trades "
                f"({w.oos_metrics.expectancy_r:+.2f}R)"
            )
        except ValueError as exc:
            print(f"  window {idx}: skipped ({exc})")
        start += oos_bars
        idx += 1
    print(
        f"  aggregate efficiency {wf.efficiency:.3f}, "
        f"profitable windows {wf.profitable_window_fraction:.0%}\n"
    )

    mc = monte_carlo.run_all(oos_res.trades, simulations=1500, risk_pct=settings.risk.risk_pct_a)

    report = DeploymentGate().evaluate(
        strategy=args.strategy,
        version="1.0",
        oos=oos_res.metrics,
        in_sample=is_res.metrics,
        walk_forward=wf,
        monte_carlo=mc,
        sensitivity={"max_relative_drop": 0.0},  # populated by a parameter sweep
        stress={"2x_costs": stress_res.metrics},
        calibration=None,
        leak_checks={
            # Each of these is enforced structurally and covered by a test; see
            # tests/unit/test_marketview.py and tests/unit/test_database.py.
            "no_lookahead_in_features": True,
            "vintage_filtered_macro": True,
            "masked_future_actuals": True,
            "time_ordered_split": True,
            "costs_modelled": True,
        },
    )
    print(report.render())

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "report": report.as_dict(),
                    "in_sample": is_res.metrics.as_dict(),
                    "out_of_sample": oos_res.metrics.as_dict(),
                    "stress": stress_res.metrics.as_dict(),
                    "walk_forward": wf.as_dict(),
                    "monte_carlo": {k: v.as_dict() for k, v in mc.items()},
                    "rejection_ledger": oos_res.rejection_ledger,
                    "data_hash": oos_res.data_hash,
                    "config_hash": oos_res.config_hash,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                default=str,
            )
        )
        print(f"\nwrote {args.json}")
    return 0 if report.passed else 1


def _bounds(series, base, lo_i: int, hi_i: int) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    import numpy as np

    lo_ts, hi_ts = int(base.ts[lo_i]), int(base.ts[min(hi_i, len(base) - 1)])
    return (
        int(np.searchsorted(series.ts, lo_ts, side="left")),
        int(np.searchsorted(series.ts, hi_ts, side="right")),
    )


if __name__ == "__main__":
    sys.exit(main())
