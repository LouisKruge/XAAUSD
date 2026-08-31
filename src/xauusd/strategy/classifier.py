"""A / A+ / NO_TRADE classification.

Classification is a CONJUNCTION of independent requirements, not a score threshold:

    all hard gates pass
  AND score >= class minimum
  AND calibrated probability >= class minimum
  AND reward-to-risk >= 2.0
  AND enough INDEPENDENT categories scored strong      <- breadth, not just total
  AND no higher-timeframe conflict
  AND (A+ only) macro alignment is known and aligned
  AND (A+ only) news risk is LOW
  AND the strategy itself has passed out-of-sample validation

The breadth requirement exists because a weighted sum can be inflated by one very
strong signal. Requiring several independent categories to be strong is a much better
proxy for genuine confluence.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.config.settings import Settings
from xauusd.domain.enums import Classification, NewsRisk, ValidationStatus
from xauusd.domain.types import GateResult, MarketSnapshot, ScoreBreakdown, TradePlan
from xauusd.strategy.features import FeatureVector


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: Classification
    checks: tuple[GateResult, ...]
    reason: str

    @property
    def is_trade(self) -> bool:
        return self.classification is not Classification.NO_TRADE

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.passed)


class Classifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def classify(
        self,
        breakdown: ScoreBreakdown,
        probability: float | None,
        features: FeatureVector,
        snap: MarketSnapshot,
        plan: TradePlan,
        gates_passed: bool,
        strategy_status: ValidationStatus = ValidationStatus.DEV,
        model_healthy: bool = True,
    ) -> ClassificationResult:
        t = self.settings.thresholds

        if not gates_passed:
            return ClassificationResult(Classification.NO_TRADE, (), "a mandatory gate failed")

        # Probability handling. When no model is available the system degrades to
        # score-only and caps at A, rather than either refusing to trade or pretending
        # to have a probability it does not have.
        # Bound once, so "a probability we may use" is a single value rather than a
        # flag that every later branch has to remember to pair with the value.
        prob: float | None = probability if model_healthy else None
        if t.require_probability_model and prob is None:
            return ClassificationResult(
                Classification.NO_TRADE,
                (),
                "probability model required by configuration but unavailable",
            )

        checks_aplus: list[GateResult] = [
            GateResult(
                "score_a_plus",
                breakdown.total >= t.a_plus_score_min,
                round(breakdown.total, 2),
                t.a_plus_score_min,
            ),
            GateResult(
                "categories_a_plus",
                len(breakdown.strong_categories) >= t.a_plus_strong_categories_min,
                len(breakdown.strong_categories),
                t.a_plus_strong_categories_min,
                detail=", ".join(breakdown.strong_categories),
            ),
            GateResult("rr_a_plus", plan.rr >= t.min_rr, round(plan.rr, 2), t.min_rr),
            GateResult(
                "macro_alignment_a_plus",
                bool(features.macro_known and features.macro_aligned),
                str(snap.macro.bias),
                "known and aligned",
            ),
            GateResult(
                "news_risk_a_plus",
                snap.news.risk is NewsRisk.LOW,
                str(snap.news.risk),
                str(NewsRisk.LOW),
            ),
            GateResult(
                "no_htf_conflict_a_plus",
                not features.htf_conflict,
                bool(features.htf_conflict),
                False,
            ),
            GateResult("mss_required_a_plus", bool(features.has_mss), bool(features.has_mss), True),
            GateResult(
                "sweep_required_a_plus",
                features.sweep_quality > 0.5,
                round(features.sweep_quality, 3),
                0.5,
            ),
            GateResult(
                "strategy_status_a_plus",
                strategy_status.live_eligible,
                str(strategy_status),
                "OOS_PASSED or better",
            ),
        ]
        if prob is not None:
            checks_aplus.append(
                GateResult(
                    "probability_a_plus",
                    prob >= t.a_plus_probability_min,
                    round(prob, 4),
                    t.a_plus_probability_min,
                )
            )
        else:
            checks_aplus.append(
                GateResult(
                    "probability_a_plus",
                    False,
                    "model unavailable",
                    t.a_plus_probability_min,
                    detail="A+ requires a calibrated probability; degraded to A-only",
                )
            )

        if all(c.passed for c in checks_aplus):
            return ClassificationResult(
                Classification.A_PLUS,
                tuple(checks_aplus),
                f"exceptional confluence: {breakdown.total:.1f}/100 across "
                f"{len(breakdown.strong_categories)} strong categories",
            )

        checks_a: list[GateResult] = [
            GateResult(
                "score_a",
                breakdown.total >= t.a_score_min,
                round(breakdown.total, 2),
                t.a_score_min,
            ),
            GateResult(
                "categories_a",
                len(breakdown.strong_categories) >= t.a_strong_categories_min,
                len(breakdown.strong_categories),
                t.a_strong_categories_min,
                detail=", ".join(breakdown.strong_categories),
            ),
            GateResult("rr_a", plan.rr >= t.min_rr, round(plan.rr, 2), t.min_rr),
            GateResult(
                "no_htf_conflict_a", not features.htf_conflict, bool(features.htf_conflict), False
            ),
            GateResult(
                "macro_not_opposed_a",
                not (features.macro_known and features.macro_bias * features.direction < 0),
                str(snap.macro.bias),
                "not opposing",
            ),
            GateResult(
                "news_risk_a",
                snap.news.risk.level <= NewsRisk.MODERATE.level,
                str(snap.news.risk),
                f"<= {NewsRisk.MODERATE}",
            ),
            GateResult(
                "structure_required_a",
                bool(features.has_mss or features.has_bos),
                f"mss={bool(features.has_mss)} bos={bool(features.has_bos)}",
                True,
            ),
        ]
        if prob is not None:
            checks_a.append(
                GateResult(
                    "probability_a",
                    prob >= t.a_probability_min,
                    round(prob, 4),
                    t.a_probability_min,
                )
            )

        if all(c.passed for c in checks_a):
            downgraded = [c.name for c in checks_aplus if not c.passed]
            return ClassificationResult(
                Classification.A,
                tuple(checks_a),
                f"high quality: {breakdown.total:.1f}/100; not A+ because "
                f"{', '.join(downgraded[:3])}",
            )

        failed = [c.name for c in checks_a if not c.passed]
        return ClassificationResult(
            Classification.NO_TRADE,
            tuple(checks_a),
            f"below A threshold: {', '.join(failed)}",
        )

    def risk_pct_for(self, classification: Classification) -> float:
        """The CEILING for this class. The sizing layer takes the minimum of several."""
        r = self.settings.risk
        if classification is Classification.A_PLUS:
            return min(r.risk_pct_a_plus, r.global_risk_cap_pct)
        if classification is Classification.A:
            return min(r.risk_pct_a, r.global_risk_cap_pct)
        return 0.0
