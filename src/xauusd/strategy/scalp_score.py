"""The scalp score: 0-100, separate from the A/A+ score and validated separately.

The A/A+ score asks "is this an exceptional opportunity?" and answers by counting
confluences. A short-duration engine asks a different question — "is this a
statistically valid short-duration opportunity?" — and a scorer built for the first
question answers the second badly: it rewards the slow, heavy confirmations that a
setup lasting ten minutes cannot accumulate.

So this is a separate scorer with separate weights. What it is NOT is a lower bar for
the same thing: the hard gates (cost, net expectancy, risk, correlation) apply to every
signal regardless of score, and a 100/100 signal whose costs exceed its target is still
refused. The score decides between candidates that have already proved they are
affordable; it can never make an unaffordable one acceptable.

The weights below are an initial hypothesis. They are configuration precisely so the
out-of-sample sweep can replace them with measured ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from xauusd.config.settings import ScalpScoreWeights
from xauusd.strategy.scalp.base import ScalpFactors, clamp01


@dataclass(frozen=True, slots=True)
class ScalpScore:
    total: float
    contributions: dict[str, float]
    weights: dict[str, float]

    @property
    def strongest(self) -> list[str]:
        """Factors delivering at least 70% of their available weight."""
        return sorted(
            (
                k
                for k, v in self.contributions.items()
                if self.weights.get(k, 0) > 0 and v / self.weights[k] >= 0.70
            ),
            key=lambda k: -self.contributions[k],
        )

    @property
    def weakest(self) -> list[str]:
        """Factors delivering under 30%. The journal's answer to 'why only 61?'"""
        return sorted(
            (
                k
                for k, v in self.contributions.items()
                if self.weights.get(k, 0) > 0 and v / self.weights[k] < 0.30
            ),
            key=lambda k: self.contributions[k],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "total": round(self.total, 2),
            "contributions": {k: round(v, 2) for k, v in self.contributions.items()},
            "strongest": self.strongest,
            "weakest": self.weakest,
        }


class ScalpScorer:
    def __init__(self, weights: ScalpScoreWeights | None = None) -> None:
        self.weights = weights or ScalpScoreWeights()

    def _weight_map(self) -> dict[str, float]:
        w = self.weights
        return {
            "market_structure": w.market_structure,
            "liquidity": w.liquidity,
            "momentum": w.momentum,
            "entry_location": w.entry_location,
            "volatility": w.volatility,
            "session": w.session,
            "dxy": w.dxy,
            "news": w.news,
            "htf_context": w.htf_context,
        }

    def score(self, factors: ScalpFactors) -> ScalpScore:
        """Weighted sum of normalised factors. Total cannot exceed 100 by construction.

        Each factor is 0..1 and the weights are validated to sum to 100, so the score
        is bounded by construction.

        `clamp01` rather than `max(0.0, min(1.0, x))`, and the difference is not
        cosmetic: in Python `min(1.0, nan)` returns 1.0, so the naive form awards a NaN
        factor FULL marks. A warm-up NaN would then inflate the score instead of
        suppressing it — failing open, in the one place where the score decides whether
        to risk money. clamp01 sends NaN to zero.
        """
        weights = self._weight_map()
        values = factors.as_dict()
        contributions = {
            name: weight * clamp01(values.get(name, 0.0)) for name, weight in weights.items()
        }
        return ScalpScore(
            total=sum(contributions.values()),
            contributions=contributions,
            weights=weights,
        )
