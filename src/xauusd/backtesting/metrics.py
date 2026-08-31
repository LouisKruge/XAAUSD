"""Performance metrics.

The reporting unit is the R-multiple throughout. Currency P&L is a side effect of
account size; R-multiples let a 2019 trade and a 2026 trade be compared directly.

The metric that matters most for the deployment gate is `win_rate_wilson_lower_95`,
not `win_rate`. Seven wins in ten trades is not evidence of a 70% strategy — the Wilson
lower bound on that sample is about 40%. Judging a gate on the point estimate is how a
system gets deployed on noise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from xauusd.domain.types import ClosedTrade


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — correct for small samples, unlike the normal approximation."""
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def max_drawdown(equity: Sequence[float]) -> tuple[float, int, int]:
    """(max drawdown fraction, peak index, trough index) from an equity curve."""
    if len(equity) < 2:
        return 0.0, 0, 0
    arr = np.asarray(equity, dtype=float)
    running_peak = np.maximum.accumulate(arr)
    dd = np.divide(
        running_peak - arr,
        running_peak,
        out=np.zeros_like(arr),
        where=running_peak > 0,
    )
    trough = int(np.argmax(dd))
    peak = int(np.argmax(arr[: trough + 1])) if trough > 0 else 0
    return float(dd[trough]), peak, trough


def risk_of_ruin(
    win_rate: float,
    avg_win_r: float,
    avg_loss_r: float,
    risk_pct: float,
    ruin_fraction: float = 0.5,
    simulations: int = 4000,
    trades: int = 500,
    seed: int = 11,
) -> float:
    """Monte Carlo probability of losing `ruin_fraction` of the account.

    Simulated rather than closed-form because the closed-form formula assumes fixed
    fractional betting with a constant win/loss size, which is not what this system does.
    """
    if win_rate <= 0 or avg_loss_r <= 0 or risk_pct <= 0:
        return 0.0
    rng = np.random.default_rng(seed)
    ruined = 0
    for _ in range(simulations):
        equity = 1.0
        floor = 1.0 - ruin_fraction
        for _ in range(trades):
            r = avg_win_r if rng.random() < win_rate else -avg_loss_r
            equity *= 1.0 + r * risk_pct
            if equity <= floor:
                ruined += 1
                break
    return ruined / simulations


@dataclass(slots=True)
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    win_rate: float = 0.0
    win_rate_wilson_lower_95: float = 0.0
    win_rate_wilson_upper_95: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    expectancy_money: float = 0.0
    total_r: float = 0.0
    total_pnl: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    avg_rr_realised: float = 0.0  # avg_win_r / avg_loss_r — the payoff ratio
    avg_rr_planned: float = 0.0  # what the plans targeted, from the trade plan
    avg_rr_travelled: float = 0.0  # how far price actually went, in initial-risk units
    max_drawdown_pct: float = 0.0
    max_drawdown_r: float = 0.0
    max_drawdown_duration_trades: int = 0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    recovery_factor: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    risk_of_ruin: float = 0.0
    ulcer_index: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    avg_bars_held: float = 0.0
    trades_per_month: float = 0.0
    exposure_pct: float = 0.0
    by_session: dict[str, Any] = field(default_factory=dict)
    by_regime: dict[str, Any] = field(default_factory=dict)
    by_class: dict[str, Any] = field(default_factory=dict)
    by_day_of_week: dict[str, Any] = field(default_factory=dict)
    by_hour: dict[str, Any] = field(default_factory=dict)
    by_year: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def summary_line(self) -> str:
        return (
            f"{self.trades} trades | win {self.win_rate:.1%} "
            f"(95% lower {self.win_rate_wilson_lower_95:.1%}) | "
            f"PF {self.profit_factor:.2f} | expectancy {self.expectancy_r:+.3f}R | "
            f"maxDD {self.max_drawdown_pct:.2%} | Sharpe {self.sharpe:.2f}"
        )


def _group(trades: list[ClosedTrade], key) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    buckets: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        buckets.setdefault(str(key(t)), []).append(t)
    out: dict[str, Any] = {}
    for name, group in sorted(buckets.items()):
        rs = [t.r_multiple for t in group]
        wins = sum(1 for t in group if t.is_win)
        lo, _ = wilson_interval(wins, len(group))
        out[name] = {
            "trades": len(group),
            "win_rate": round(wins / len(group), 4),
            "win_rate_lower_95": round(lo, 4),
            "expectancy_r": round(float(np.mean(rs)), 4),
            "total_r": round(float(np.sum(rs)), 3),
        }
    return out


def compute(
    trades: list[ClosedTrade],
    starting_equity: float = 10_000.0,
    equity_curve: Sequence[float] | None = None,
    risk_pct: float = 0.01,
    period_days: float | None = None,
    bars_in_market: int = 0,
    total_bars: int = 0,
) -> Metrics:
    m = Metrics()
    if not trades:
        return m

    rs = np.array([t.r_multiple for t in trades], dtype=float)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)

    m.trades = len(trades)
    m.wins = int(sum(1 for t in trades if t.is_win))
    m.losses = int(sum(1 for t in trades if t.is_loss))
    m.breakevens = m.trades - m.wins - m.losses
    decided = m.wins + m.losses

    # Break-even exits are neither wins nor losses; counting them as losses would
    # understate a system that moves to break-even, and as wins would flatter it.
    m.win_rate = m.wins / decided if decided else 0.0
    lo, hi = wilson_interval(m.wins, decided)
    m.win_rate_wilson_lower_95, m.win_rate_wilson_upper_95 = lo, hi

    gross_profit = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls < 0].sum())
    m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    m.expectancy_r = float(rs.mean())
    m.expectancy_money = float(pnls.mean())
    m.total_r = float(rs.sum())
    m.total_pnl = float(pnls.sum())

    win_rs = rs[rs > 0.05]
    loss_rs = rs[rs < -0.05]
    m.avg_win_r = float(win_rs.mean()) if win_rs.size else 0.0
    m.avg_loss_r = float(abs(loss_rs.mean())) if loss_rs.size else 0.0
    m.avg_rr_realised = m.avg_win_r / m.avg_loss_r if m.avg_loss_r > 0 else 0.0
    planned = [t.planned_rr for t in trades if t.planned_rr > 0]
    m.avg_rr_planned = float(np.mean(planned)) if planned else 0.0
    travelled = [t.realised_rr for t in trades if t.realised_rr > 0]
    m.avg_rr_travelled = float(np.mean(travelled)) if travelled else 0.0

    curve = list(equity_curve) if equity_curve else _equity_from_trades(trades, starting_equity)
    dd, peak_i, trough_i = max_drawdown(curve)
    m.max_drawdown_pct = dd
    m.max_drawdown_duration_trades = max(0, trough_i - peak_i)
    r_curve = np.concatenate(([0.0], np.cumsum(rs)))
    r_peak = np.maximum.accumulate(r_curve)
    m.max_drawdown_r = float(np.max(r_peak - r_curve))

    # len BEFORE std: std(ddof=1) on a single trade divides by zero and returns nan
    # (with a RuntimeWarning) before the guard that exists to prevent exactly that.
    if len(rs) > 1 and rs.std(ddof=1) > 0:
        # Per-trade Sharpe annualised by trade frequency, which is the honest version
        # for an irregular-frequency system.
        per_trade = rs.mean() / rs.std(ddof=1)
        trades_per_year = (
            len(trades) / (period_days / 365.25) if period_days and period_days > 0 else len(trades)
        )
        m.sharpe = float(per_trade * math.sqrt(max(trades_per_year, 1.0)))

        # Sortino uses the TEXTBOOK downside deviation — the root-mean-square of
        # min(r - target, 0) over ALL trades — not the standard deviation of the
        # losing subset.
        #
        # This distinction is critical for a fixed-stop system, not academic. Every
        # loss is approximately -1R by construction, so the losing subset has almost
        # no dispersion: on a real 12-trade sample here, std(losses) = 0.005 gives a
        # Sortino of 182, while the correct downside deviation of 0.662 gives 1.51.
        # The deployment gate requires Sortino >= 2.0, so the wrong formula would wave
        # through a strategy the right one blocks.
        downside = np.minimum(rs - 0.0, 0.0)
        downside_deviation = float(np.sqrt(np.mean(downside**2)))
        m.sortino = (
            float(rs.mean() / downside_deviation * math.sqrt(max(trades_per_year, 1.0)))
            if downside_deviation > 1e-9
            else 0.0
        )

    total_return = (curve[-1] / starting_equity - 1.0) if curve else 0.0
    m.calmar = total_return / dd if dd > 0 else 0.0
    m.recovery_factor = m.total_pnl / (dd * starting_equity) if dd > 0 else 0.0

    streak = best_loss = best_win = 0
    for t in trades:
        if t.is_loss:
            streak = streak - 1 if streak < 0 else -1
            best_loss = max(best_loss, -streak)
        elif t.is_win:
            streak = streak + 1 if streak > 0 else 1
            best_win = max(best_win, streak)
    m.max_consecutive_losses = best_loss
    m.max_consecutive_wins = best_win

    m.risk_of_ruin = risk_of_ruin(m.win_rate, m.avg_win_r, m.avg_loss_r, risk_pct)

    arr = np.asarray(curve, dtype=float)
    running = np.maximum.accumulate(arr)
    drawdowns = np.divide(running - arr, running, out=np.zeros_like(arr), where=running > 0)
    m.ulcer_index = float(np.sqrt(np.mean(drawdowns**2)))

    m.avg_mae_r = float(np.mean([t.mae_r for t in trades]))
    m.avg_mfe_r = float(np.mean([t.mfe_r for t in trades]))
    m.avg_bars_held = float(np.mean([t.bars_held for t in trades]))
    if period_days and period_days > 0:
        m.trades_per_month = len(trades) / (period_days / 30.44)
    if total_bars > 0:
        m.exposure_pct = bars_in_market / total_bars

    m.by_session = _group(trades, lambda t: t.session)
    m.by_regime = _group(trades, lambda t: t.regime)
    m.by_class = _group(trades, lambda t: t.classification)
    m.by_day_of_week = _group(trades, lambda t: t.opened_at.strftime("%a"))
    m.by_hour = _group(trades, lambda t: f"{t.opened_at.hour:02d}")
    m.by_year = _group(trades, lambda t: t.opened_at.year)
    return m


def _equity_from_trades(trades: list[ClosedTrade], starting: float) -> list[float]:
    equity = [starting]
    for t in trades:
        equity.append(equity[-1] + t.net_pnl)
    return equity
