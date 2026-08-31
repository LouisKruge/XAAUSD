"""The deployment gate.

A strategy version reaches OOS_PASSED only by clearing every criterion below on data it
was never fitted on. Passing makes it ELIGIBLE for live routing; it is not a prediction
that live performance will match.

--------------------------------------------------------------------------------------
A NOTE ON THE 70% WIN-RATE REQUIREMENT — read this before changing the numbers
--------------------------------------------------------------------------------------
The brief requires a strategy to "demonstrate at least a 70% win rate in robust
validation". There are two defensible readings and they are very different in practice:

  (a) OBSERVED win rate >= 70%.
      Achievable, but 7 wins in 10 trades satisfies it, which is meaningless.

  (b) The 95% Wilson LOWER BOUND >= 70%.
      Statistically honest, but the arithmetic is brutal: even 700 wins in 1000 trades —
      exactly 70% observed — has a lower bound of 67.1% and FAILS. To clear a 70% lower
      bound you need roughly 74% observed over 200 trades, or 72.8% over 1000.

Implementing (b) alone would make the gate almost unreachable and would quietly mean
"never deploy". Implementing (a) alone would let a 10-trade fluke through. So the gate
requires BOTH:

    observed win rate            >= 0.70      (the brief's stated bar)
    Wilson 95% lower bound       >= 0.60      (evidence it is not a small-sample fluke)
    out-of-sample trades         >= 100       (below this nothing is measurable)

That combination is the faithful reading: it enforces the 70% requirement while also
requiring the sample to be large enough for the number to mean something. Both
thresholds are configuration, and the report always prints the observed rate, the
bound, and the sample size, so the decision is never hidden inside a single pass/fail.

Expect most strategy versions to FAIL this gate. That is the gate working, not a bug.
And a variant that clears every criterion on the first attempt should be treated as a
suspected data leak until proven otherwise — hence the mandatory leak checks below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xauusd.backtesting.metrics import Metrics
from xauusd.backtesting.monte_carlo import MonteCarloResult
from xauusd.backtesting.walk_forward import WalkForwardResult
from xauusd.domain.enums import ValidationStatus


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    passed: bool
    observed: Any
    required: Any
    rationale: str = ""
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.name,
            "passed": self.passed,
            "observed": (
                round(self.observed, 6) if isinstance(self.observed, float) else self.observed
            ),
            "required": self.required,
            "blocking": self.blocking,
            "rationale": self.rationale,
        }


@dataclass(slots=True)
class GateThresholds:
    min_oos_trades: int = 100
    min_win_rate_observed: float = 0.70
    min_win_rate_lower_bound: float = 0.60
    min_profit_factor: float = 2.0
    min_expectancy_r: float = 0.40
    max_drawdown_pct: float = 0.15
    min_avg_rr_realised: float = 1.80
    min_sharpe: float = 1.50
    min_sortino: float = 2.00
    max_consecutive_losses: int = 8
    max_risk_of_ruin: float = 0.01
    min_walk_forward_efficiency: float = 0.50
    min_profitable_window_fraction: float = 0.70
    min_mc_p5_equity_ratio: float = 1.00  # 5th percentile must still be above start
    max_sensitivity_cliff: float = 0.50  # performance must degrade smoothly
    stress_spread_multiple: float = 2.0
    stress_slippage_multiple: float = 2.0
    max_calibration_brier: float = 0.25
    calibration_slope_range: tuple[float, float] = (0.8, 1.2)


@dataclass(slots=True)
class ValidationReport:
    strategy: str
    version: str
    created_at: datetime
    criteria: list[Criterion] = field(default_factory=list)
    verdict: ValidationStatus = ValidationStatus.FAILED
    approved_regimes: list[str] = field(default_factory=list)
    approved_sessions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria if c.blocking)

    @property
    def failures(self) -> list[Criterion]:
        return [c for c in self.criteria if c.blocking and not c.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "verdict": str(self.verdict),
            "passed": self.passed,
            "criteria": [c.as_dict() for c in self.criteria],
            "failures": [c.name for c in self.failures],
            "approved_regimes": self.approved_regimes,
            "approved_sessions": self.approved_sessions,
            "notes": self.notes,
        }

    def render(self) -> str:
        lines = [
            f"VALIDATION REPORT — {self.strategy} v{self.version}",
            f"  generated {self.created_at.isoformat()}",
            f"  VERDICT: {self.verdict}",
            "",
        ]
        width = max((len(c.name) for c in self.criteria), default=10)
        for c in self.criteria:
            mark = "PASS" if c.passed else ("FAIL" if c.blocking else "warn")
            obs = f"{c.observed:.4f}" if isinstance(c.observed, float) else str(c.observed)
            lines.append(f"  [{mark}] {c.name:<{width}}  observed {obs:<12} required {c.required}")
            if c.rationale and not c.passed:
                lines.append(f"         {c.rationale}")
        if self.failures:
            lines.append("")
            lines.append(f"  BLOCKED BY: {', '.join(c.name for c in self.failures)}")
        if self.notes:
            lines.append("")
            lines.extend(f"  NOTE: {n}" for n in self.notes)
        return "\n".join(lines)


class DeploymentGate:
    def __init__(self, thresholds: GateThresholds | None = None) -> None:
        self.t = thresholds or GateThresholds()

    def evaluate(
        self,
        strategy: str,
        version: str,
        oos: Metrics,
        in_sample: Metrics | None = None,
        walk_forward: WalkForwardResult | None = None,
        monte_carlo: dict[str, MonteCarloResult] | None = None,
        sensitivity: dict[str, Any] | None = None,
        stress: dict[str, Metrics] | None = None,
        calibration: dict[str, float] | None = None,
        leak_checks: dict[str, bool] | None = None,
        starting_equity: float = 10_000.0,
    ) -> ValidationReport:
        t = self.t
        c: list[Criterion] = []

        # --- 1. sample size ----------------------------------------------------------
        c.append(
            Criterion(
                "oos_sample_size",
                oos.trades >= t.min_oos_trades,
                oos.trades,
                f">= {t.min_oos_trades}",
                "below this the win rate is not measurable at any confidence",
            )
        )

        # --- 2. the win-rate requirement, both readings -------------------------------
        c.append(
            Criterion(
                "win_rate_observed",
                oos.win_rate >= t.min_win_rate_observed,
                oos.win_rate,
                f">= {t.min_win_rate_observed:.0%}",
                "the brief's stated bar",
            )
        )
        c.append(
            Criterion(
                "win_rate_lower_bound",
                oos.win_rate_wilson_lower_95 >= t.min_win_rate_lower_bound,
                oos.win_rate_wilson_lower_95,
                f">= {t.min_win_rate_lower_bound:.0%}",
                "95% Wilson lower bound — evidence the rate is not a small-sample fluke",
            )
        )

        # --- 3-7. the economics ------------------------------------------------------
        c.append(
            Criterion(
                "profit_factor",
                oos.profit_factor >= t.min_profit_factor,
                oos.profit_factor,
                f">= {t.min_profit_factor}",
            )
        )
        c.append(
            Criterion(
                "expectancy_r",
                oos.expectancy_r >= t.min_expectancy_r,
                oos.expectancy_r,
                f">= {t.min_expectancy_r}R",
            )
        )
        c.append(
            Criterion(
                "max_drawdown",
                oos.max_drawdown_pct <= t.max_drawdown_pct,
                oos.max_drawdown_pct,
                f"<= {t.max_drawdown_pct:.0%}",
            )
        )
        c.append(
            Criterion(
                "avg_rr_realised",
                oos.avg_rr_realised >= t.min_avg_rr_realised,
                oos.avg_rr_realised,
                f">= {t.min_avg_rr_realised}",
            )
        )
        c.append(Criterion("sharpe", oos.sharpe >= t.min_sharpe, oos.sharpe, f">= {t.min_sharpe}"))
        c.append(
            Criterion("sortino", oos.sortino >= t.min_sortino, oos.sortino, f">= {t.min_sortino}")
        )
        c.append(
            Criterion(
                "consecutive_losses",
                oos.max_consecutive_losses <= t.max_consecutive_losses,
                oos.max_consecutive_losses,
                f"<= {t.max_consecutive_losses}",
                "a streak longer than this must still be survivable at 2% risk",
            )
        )
        c.append(
            Criterion(
                "risk_of_ruin",
                oos.risk_of_ruin <= t.max_risk_of_ruin,
                oos.risk_of_ruin,
                f"<= {t.max_risk_of_ruin:.1%}",
            )
        )

        # --- 8-9. walk-forward --------------------------------------------------------
        if walk_forward is not None:
            eff = walk_forward.efficiency
            c.append(
                Criterion(
                    "walk_forward_efficiency",
                    eff >= t.min_walk_forward_efficiency,
                    eff,
                    f">= {t.min_walk_forward_efficiency}",
                    "out-of-sample expectancy relative to in-sample; low means curve fitting",
                )
            )
            c.append(
                Criterion(
                    "profitable_windows",
                    walk_forward.profitable_window_fraction >= t.min_profitable_window_fraction,
                    walk_forward.profitable_window_fraction,
                    f">= {t.min_profitable_window_fraction:.0%}",
                )
            )
        else:
            c.append(Criterion("walk_forward_efficiency", False, "not run", "required"))

        # --- 10. Monte Carlo ----------------------------------------------------------
        if monte_carlo:
            boot = monte_carlo.get("bootstrap")
            shuf = monte_carlo.get("shuffle")
            if boot:
                ratio = boot.final_equity_p5 / starting_equity
                c.append(
                    Criterion(
                        "monte_carlo_p5_equity",
                        ratio >= t.min_mc_p5_equity_ratio,
                        ratio,
                        f">= {t.min_mc_p5_equity_ratio}",
                        "5th percentile of resampled outcomes must still be above break-even",
                    )
                )
            if shuf:
                c.append(
                    Criterion(
                        "monte_carlo_drawdown",
                        shuf.max_drawdown_p95 <= t.max_drawdown_pct * 1.5,
                        shuf.max_drawdown_p95,
                        f"<= {t.max_drawdown_pct * 1.5:.0%}",
                        "95th-percentile drawdown across trade orderings",
                    )
                )
        else:
            c.append(Criterion("monte_carlo_p5_equity", False, "not run", "required"))

        # --- 11. parameter sensitivity ------------------------------------------------
        if sensitivity is not None:
            cliff = float(sensitivity.get("max_relative_drop", 1.0))
            c.append(
                Criterion(
                    "parameter_sensitivity",
                    cliff <= t.max_sensitivity_cliff,
                    cliff,
                    f"<= {t.max_sensitivity_cliff}",
                    "performance must degrade smoothly; an isolated peak is curve fitting",
                )
            )
        else:
            c.append(Criterion("parameter_sensitivity", False, "not run", "required"))

        # --- 12. cost stress ----------------------------------------------------------
        if stress:
            worst = min((m.expectancy_r for m in stress.values()), default=-1.0)
            worst_pf = min((m.profit_factor for m in stress.values()), default=0.0)
            c.append(
                Criterion(
                    "stress_expectancy",
                    worst > 0,
                    worst,
                    "> 0R",
                    f"expectancy at {t.stress_spread_multiple}x spread and "
                    f"{t.stress_slippage_multiple}x slippage",
                )
            )
            c.append(
                Criterion(
                    "stress_profit_factor",
                    worst_pf >= 1.3,
                    worst_pf,
                    ">= 1.3",
                    "the edge must survive realistic cost deterioration",
                )
            )
        else:
            c.append(Criterion("stress_expectancy", False, "not run", "required"))

        # --- 13. probability calibration ----------------------------------------------
        if calibration:
            brier = float(calibration.get("brier", 1.0))
            slope = float(calibration.get("slope", 0.0))
            lo, hi = t.calibration_slope_range
            c.append(
                Criterion(
                    "calibration_brier",
                    brier <= t.max_calibration_brier,
                    brier,
                    f"<= {t.max_calibration_brier}",
                )
            )
            c.append(
                Criterion(
                    "calibration_slope",
                    lo <= slope <= hi,
                    slope,
                    f"in [{lo}, {hi}]",
                    "a slope far from 1 means the probabilities are miscalibrated",
                )
            )
        else:
            c.append(
                Criterion(
                    "calibration_brier",
                    True,
                    "no model",
                    "n/a",
                    blocking=False,
                    rationale="no probability model; system runs score-only in A-only mode",
                )
            )

        # --- 14. mandatory leak hunt ---------------------------------------------------
        checks = leak_checks or {}
        for name, description in (
            ("no_lookahead_in_features", "no feature reads data after the decision bar"),
            ("vintage_filtered_macro", "macro reads filtered on release_ts, not ref_date"),
            ("masked_future_actuals", "calendar actuals masked for unreleased events"),
            ("time_ordered_split", "in-sample/out-of-sample split is chronological"),
            ("costs_modelled", "spread, commission and slippage all applied"),
        ):
            c.append(
                Criterion(
                    f"leak_check.{name}",
                    bool(checks.get(name, False)),
                    checks.get(name, False),
                    True,
                    description,
                )
            )

        # --- 15. in-sample degradation sanity -----------------------------------------
        if in_sample is not None and in_sample.trades:
            drop = (
                (in_sample.expectancy_r - oos.expectancy_r) / abs(in_sample.expectancy_r)
                if in_sample.expectancy_r
                else 0.0
            )
            c.append(
                Criterion(
                    "is_oos_degradation",
                    drop <= 0.60,
                    drop,
                    "<= 0.60",
                    "out-of-sample expectancy collapsing versus in-sample indicates fitting",
                )
            )

        report = ValidationReport(strategy, version, datetime.now(UTC), c)
        report.verdict = ValidationStatus.OOS_PASSED if report.passed else ValidationStatus.FAILED
        if report.passed:
            report.notes.append(
                "PASSED. Treat a first-attempt pass as a suspected data leak until the "
                "leak checks have been independently re-verified. Eligibility is not a "
                "prediction of live performance."
            )
        else:
            report.notes.append(
                f"FAILED on {len(report.failures)} criteria. This is the expected outcome "
                f"for most strategy versions and is the gate working as designed."
            )
        # Sessions and regimes are approved from where the strategy actually performed,
        # not from what its author hoped.
        report.approved_sessions = [
            s for s, v in oos.by_session.items() if v["trades"] >= 20 and v["expectancy_r"] > 0
        ]
        report.approved_regimes = [
            r for r, v in oos.by_regime.items() if v["trades"] >= 20 and v["expectancy_r"] > 0
        ]
        return report
