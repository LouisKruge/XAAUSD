"""Monte Carlo robustness testing.

Three independent resamplings, because they answer different questions:

  * TRADE ORDER SHUFFLE — "was the equity curve's shape luck?" Same trades, different
    sequence. Reveals how bad the drawdown could have been with the same edge.
  * BOOTSTRAP — "was the sample of trades itself lucky?" Resamples with replacement,
    giving a distribution over expectancy and win rate.
  * RANDOM START — "does the result depend on when you started?" Runs contiguous
    sub-windows.

The gate criterion that matters is the 5th percentile of final equity: if a strategy is
underwater at the 5th percentile, it is not deployable regardless of its mean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from xauusd.backtesting.metrics import max_drawdown
from xauusd.domain.types import ClosedTrade


@dataclass(slots=True)
class MonteCarloResult:
    simulations: int
    final_equity_mean: float
    final_equity_p5: float
    final_equity_p25: float
    final_equity_median: float
    final_equity_p95: float
    max_drawdown_mean: float
    max_drawdown_p95: float
    max_drawdown_worst: float
    prob_profitable: float
    prob_drawdown_exceeds_limit: float
    expectancy_p5: float
    expectancy_median: float
    win_rate_p5: float
    kind: str = "shuffle"

    def as_dict(self) -> dict[str, Any]:
        # asdict, not __dict__: this is a slots dataclass and has no __dict__, which
        # crashed JSON export of a validation report.
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _simulate_equity(r_multiples: Sequence[float], starting: float, risk_pct: float) -> np.ndarray:
    """Compound an R sequence at fixed fractional risk."""
    equity = np.empty(len(r_multiples) + 1, dtype=float)
    equity[0] = starting
    for i, r in enumerate(r_multiples):
        equity[i + 1] = equity[i] * (1.0 + r * risk_pct)
    return equity


def run(
    trades: list[ClosedTrade],
    simulations: int = 2000,
    starting_equity: float = 10_000.0,
    risk_pct: float = 0.01,
    drawdown_limit: float = 0.15,
    kind: str = "shuffle",
    seed: int = 7,
) -> MonteCarloResult:
    if len(trades) < 5:
        return MonteCarloResult(
            0,
            starting_equity,
            starting_equity,
            starting_equity,
            starting_equity,
            starting_equity,
            0,
            0,
            0,
            0,
            1.0,
            0,
            0,
            0,
            kind,
        )
    rng = np.random.default_rng(seed)
    rs = np.array([t.r_multiple for t in trades], dtype=float)
    n = len(rs)

    finals, dds, expectancies, win_rates = [], [], [], []
    for _ in range(simulations):
        if kind == "shuffle":
            sample = rng.permutation(rs)
        elif kind == "bootstrap":
            sample = rs[rng.integers(0, n, n)]
        elif kind == "random_start":
            length = max(10, int(n * 0.6))
            start = int(rng.integers(0, max(1, n - length)))
            sample = rs[start : start + length]
        else:
            raise ValueError(f"unknown kind {kind}")

        equity = _simulate_equity(sample.tolist(), starting_equity, risk_pct)
        finals.append(equity[-1])
        dds.append(max_drawdown(equity.tolist())[0])
        expectancies.append(float(sample.mean()))
        wins = int((sample > 0.05).sum())
        decided = int((np.abs(sample) > 0.05).sum())
        win_rates.append(wins / decided if decided else 0.0)

    finals_a = np.array(finals)
    dds_a = np.array(dds)
    return MonteCarloResult(
        simulations=simulations,
        final_equity_mean=float(finals_a.mean()),
        final_equity_p5=float(np.percentile(finals_a, 5)),
        final_equity_p25=float(np.percentile(finals_a, 25)),
        final_equity_median=float(np.median(finals_a)),
        final_equity_p95=float(np.percentile(finals_a, 95)),
        max_drawdown_mean=float(dds_a.mean()),
        max_drawdown_p95=float(np.percentile(dds_a, 95)),
        max_drawdown_worst=float(dds_a.max()),
        prob_profitable=float((finals_a > starting_equity).mean()),
        prob_drawdown_exceeds_limit=float((dds_a > drawdown_limit).mean()),
        expectancy_p5=float(np.percentile(expectancies, 5)),
        expectancy_median=float(np.median(expectancies)),
        win_rate_p5=float(np.percentile(win_rates, 5)),
        kind=kind,
    )


def run_all(
    trades: list[ClosedTrade],
    simulations: int = 2000,
    starting_equity: float = 10_000.0,
    risk_pct: float = 0.01,
    drawdown_limit: float = 0.15,
    seed: int = 7,
) -> dict[str, MonteCarloResult]:
    return {
        kind: run(trades, simulations, starting_equity, risk_pct, drawdown_limit, kind, seed)
        for kind in ("shuffle", "bootstrap", "random_start")
    }
