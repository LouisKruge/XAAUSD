"""The 100-point confluence score.

Two properties are deliberate and both matter more than the exact weights:

1. **The score is ORDINAL, not a probability.** It ranks candidates. The mapping from
   score to "probability this reaches +2R before -1R" is fitted separately and
   calibrated on out-of-sample outcomes.

2. **Breadth is required, not just total.** A single strong signal can inflate a
   weighted sum. Classification therefore also requires a minimum number of INDEPENDENT
   categories scoring "strong", which is a much better proxy for genuine confluence
   than a high total. `strong_categories` is computed here.

Weights are configuration and are expected to be re-optimised in Phase 10 validation.
They are NOT assumed correct.
"""

from __future__ import annotations

from xauusd.config.settings import NewsConfig, ScoringWeights, StrategyThresholds
from xauusd.domain.enums import Direction, NewsRisk
from xauusd.domain.types import MarketSnapshot, ScoreBreakdown, TradePlan
from xauusd.strategy.features import FeatureVector


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class ScoringEngine:
    def __init__(
        self,
        weights: ScoringWeights | None = None,
        thresholds: StrategyThresholds | None = None,
        news_config: NewsConfig | None = None,
    ) -> None:
        self.w = weights or ScoringWeights()
        self.t = thresholds or StrategyThresholds()
        self.news_cfg = news_config or NewsConfig()

    # -- categories --------------------------------------------------------------------

    def _htf_bias(self, f: FeatureVector) -> float:
        """Alignment across MN/W/D/H4. A conflict scores zero, not a small number."""
        if f.htf_conflict:
            return 0.0
        return self.w.htf_bias * _clamp01((f.htf_alignment + 1.0) / 2.0)

    def _market_structure(self, f: FeatureVector) -> float:
        s = 0.0
        # An MSS is the required evidence; a BOS alone is worth much less.
        if f.has_mss:
            s += 0.55 * _clamp01(f.mss_displacement_atr / 1.5)
            s += 0.10
        if f.has_bos:
            s += 0.20 * _clamp01(f.bos_displacement_atr / 1.5)
        # Structure decays: a shift 40 bars ago is not why price is here now.
        recency = _clamp01(1.0 - f.structure_age_bars / 40.0)
        s += 0.15 * recency
        return self.w.market_structure * _clamp01(s)

    def _liquidity(self, f: FeatureVector) -> float:
        s = 0.0
        s += 0.40 * f.sweep_quality
        s += 0.20 * _clamp01(f.sweep_displacement_atr / 1.5)
        s += 0.15 * f.sweep_pool_strength
        s += 0.10 * (1.0 if f.sweep_recent else 0.0)
        s += 0.15 * _clamp01(f.target_liquidity_rr / 3.0)
        return self.w.liquidity * _clamp01(s)

    def _fvg_ob(self, f: FeatureVector) -> float:
        best = max(f.fvg_quality, f.ob_quality)
        s = 0.70 * best
        if f.zone_confluence:
            s += 0.20  # FVG sitting inside an order block
        if f.ob_fresh or f.fvg_unmitigated:
            s += 0.10
        return self.w.fvg_ob * _clamp01(s)

    def _support_resistance(self, f: FeatureVector) -> float:
        s = 0.0
        s += 0.55 * _clamp01(f.sr_confluence_importance / 0.6)
        s += 0.25 * _clamp01(f.sr_confluence_count / 2.0)
        s += 0.20 * (1.0 if f.correct_side_of_range else 0.0)
        return self.w.support_resistance * _clamp01(s)

    def _fundamentals(self, f: FeatureVector, news_contribution: float) -> float:
        if not f.macro_known:
            # Unknown macro earns a small neutral credit, not a zero and not a full mark.
            base = 0.30
        else:
            base = 0.5 + 0.25 * (f.macro_bias * f.direction)
        score = self.w.fundamentals * _clamp01(base)
        # News contribution is already hard-capped by NewsConfig.
        return max(0.0, min(self.w.fundamentals, score + news_contribution))

    def _dxy_yields(self, f: FeatureVector) -> float:
        s = 0.5 * f.dxy_implication_aligned + 0.5 * f.yields_implication_aligned
        return self.w.dxy_yields * s

    def _session(self, f: FeatureVector) -> float:
        s = 0.0
        if f.session_id in (2, 3, 4):  # London, NY, overlap
            s += 0.55
        if f.session_id == 4:
            s += 0.15
        if f.killzone:
            s += 0.30
        return self.w.session * _clamp01(s)

    def _volatility_regime(self, f: FeatureVector) -> float:
        if f.regime_id == 0:  # ABNORMAL
            return 0.0
        s = 0.5 if f.regime_aligned else 0.15
        s += {0: 0.15, 1: 0.5, 2: 0.3, 3: 0.0}.get(f.vol_regime, 0.2)
        return self.w.volatility_regime * _clamp01(s)

    def _entry_confirmation(self, f: FeatureVector) -> float:
        s = 0.0
        s += 0.35 * _clamp01(f.sweep_rejection / 0.6)
        s += 0.30 * (1.0 if f.correct_side_of_range else 0.0)
        # A tight structural stop means the invalidation is close and the idea is precise.
        s += 0.20 * _clamp01(1.5 / max(f.risk_distance_atr, 0.3))
        s += 0.15 * _clamp01(1.0 - f.entry_distance_atr / 3.0)
        return self.w.entry_confirmation * _clamp01(s)

    # -- penalties ---------------------------------------------------------------------

    def _penalties(self, f: FeatureVector, snap: MarketSnapshot) -> dict[str, float]:
        w = self.w
        p: dict[str, float] = {}

        if f.news_risk >= NewsRisk.EXTREME.level:
            p["news_risk"] = w.penalty_news_risk
        elif f.news_risk >= NewsRisk.HIGH.level:
            p["news_risk"] = w.penalty_news_risk * 0.6
        elif f.news_risk >= NewsRisk.MODERATE.level:
            p["news_risk"] = w.penalty_news_risk * 0.2

        if f.macro_known and f.macro_bias * f.direction < 0:
            magnitude = abs(f.macro_bias) / 2.0
            p["fundamental_conflict"] = w.penalty_fundamental_conflict * magnitude

        if f.vol_regime == 3:
            p["poor_volatility"] = w.penalty_poor_volatility
        elif f.vol_regime == 0:
            p["poor_volatility"] = w.penalty_poor_volatility * 0.5

        if f.spread_ratio > 2.0:
            p["wide_spread"] = w.penalty_wide_spread
        elif f.spread_ratio > 1.5:
            p["wide_spread"] = w.penalty_wide_spread * 0.5

        if f.session_id in (0, 1):  # off-hours or Asia
            p["weak_session"] = w.penalty_weak_session

        # Opposing liquidity inside the stop distance means the trade is likely to be
        # stopped out on the way to being right.
        if f.opposing_liquidity_count and 0 < f.opposing_liquidity_distance_r < 1.0:
            p["opposing_liquidity"] = w.penalty_opposing_liquidity * (
                1.0 - f.opposing_liquidity_distance_r
            )

        if f.news_stale or not f.macro_known:
            p["stale_data"] = w.penalty_stale_data * (
                0.5 * f.news_stale + 0.5 * (0 if f.macro_known else 1)
            )

        if f.sr_blocking_target:
            p["blocked_target"] = w.penalty_opposing_liquidity * 0.5

        return {k: round(v, 3) for k, v in p.items() if v > 0}

    # -- top level ---------------------------------------------------------------------

    def score(
        self, f: FeatureVector, snap: MarketSnapshot, news_contribution: float = 0.0
    ) -> ScoreBreakdown:
        categories = {
            "htf_bias": self._htf_bias(f),
            "market_structure": self._market_structure(f),
            "liquidity": self._liquidity(f),
            "fvg_ob": self._fvg_ob(f),
            "support_resistance": self._support_resistance(f),
            "fundamentals": self._fundamentals(f, news_contribution),
            "dxy_yields": self._dxy_yields(f),
            "session": self._session(f),
            "volatility_regime": self._volatility_regime(f),
            "entry_confirmation": self._entry_confirmation(f),
        }
        categories = {k: round(v, 3) for k, v in categories.items()}
        maximums = self.w.category_maximums()
        penalties = self._penalties(f, snap)

        gross = sum(categories.values())
        total = max(0.0, min(100.0, gross - sum(penalties.values())))

        # Breadth: which independent categories reached the "strong" fraction of their max.
        frac = self.t.strong_category_fraction
        strong = tuple(
            k for k, v in categories.items() if maximums.get(k, 0) > 0 and v >= frac * maximums[k]
        )
        return ScoreBreakdown(
            categories=categories,
            maximums=maximums,
            penalties=penalties,
            total=round(total, 3),
            strong_categories=strong,
        )


def reasons_for_and_against(
    f: FeatureVector, breakdown: ScoreBreakdown, snap: MarketSnapshot, plan: TradePlan
) -> tuple[list[str], list[str]]:
    """Human-readable justification, stored on every decision."""
    d = plan.direction
    for_: list[str] = []
    against: list[str] = []

    if f.htf_alignment > 0.5:
        for_.append(f"higher-timeframe bias aligned ({f.htf_alignment:+.2f})")
    if f.htf_conflict:
        against.append("a higher timeframe opposes this direction")
    if f.has_mss:
        for_.append(
            f"market structure shift in {d} with {f.mss_displacement_atr:.2f} ATR displacement"
        )
    else:
        against.append("no market structure shift on the setup timeframe")
    if f.sweep_quality > 0.6:
        for_.append(
            f"liquidity sweep quality {f.sweep_quality:.2f} "
            f"(penetration {f.sweep_penetration_atr:.2f} ATR, rejection {f.sweep_rejection:.2f})"
        )
    elif f.sweep_quality > 0:
        against.append(f"liquidity sweep is weak ({f.sweep_quality:.2f})")
    else:
        against.append("no liquidity sweep preceding this setup")
    if f.fvg_quality > 0.5 or f.ob_quality > 0.5:
        for_.append(f"high-quality entry zone (FVG {f.fvg_quality:.2f} / OB {f.ob_quality:.2f})")
    if f.zone_confluence:
        for_.append("FVG and order block overlap at the entry")
    if f.correct_side_of_range:
        for_.append(
            f"entry in {'discount' if d is Direction.LONG else 'premium'} "
            f"(range position {f.premium_discount_position:.2f})"
        )
    else:
        against.append(
            f"entry on the wrong side of the dealing range "
            f"(position {f.premium_discount_position:.2f})"
        )
    if f.sr_confluence_count:
        for_.append(
            f"{f.sr_confluence_count} support/resistance level(s) at the entry "
            f"(max importance {f.sr_confluence_importance:.2f})"
        )
    if f.macro_aligned:
        for_.append(f"macro backdrop aligned ({snap.macro.bias})")
    elif f.macro_known and f.macro_bias * f.direction < 0:
        against.append(f"macro backdrop opposes this direction ({snap.macro.bias})")
    else:
        against.append("macro backdrop unknown or stale")
    if f.news_risk >= NewsRisk.HIGH.level:
        against.append(f"news risk is {snap.news.risk}")
    if f.minutes_to_next_event < 90:
        against.append(f"{snap.news.next_event_name} in {f.minutes_to_next_event:.0f} minutes")
    if f.session_id in (2, 3, 4):
        for_.append(f"{snap.session.session} session" + (" killzone" if f.killzone else ""))
    else:
        against.append(f"{snap.session.session} session is outside validated hours")
    if f.spread_ratio > 1.5:
        against.append(f"spread {f.spread_points:.0f}pts is {f.spread_ratio:.1f}x normal")
    if f.opposing_liquidity_distance_r and f.opposing_liquidity_distance_r < 1.0:
        against.append(
            f"opposing liquidity {f.opposing_liquidity_distance_r:.2f}R away, inside the stop"
        )
    if f.sr_blocking_target:
        against.append("a significant level sits between entry and target")
    for_.append(f"reward-to-risk {plan.rr:.2f} to {plan.final_target.rationale}")
    for name, value in breakdown.penalties.items():
        against.append(f"penalty: {name} (-{value:.1f})")
    return for_, against
