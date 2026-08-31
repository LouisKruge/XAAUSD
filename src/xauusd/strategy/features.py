"""Feature vector: the complete numeric description of one candidate.

This is what gets stored on every decision, what the scoring engine reads, and what the
probability model trains on. Storing it in full is what makes a decision replayable and
a model retrainable from the journal rather than from a re-run of history.

Every feature must be computable from the MarketSnapshot alone, which is itself derived
only from data at or before the evaluation instant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from xauusd.core.support_resistance import premium_discount
from xauusd.domain.enums import (
    Bias,
    Direction,
    FVGState,
    Regime,
    Session,
    Timeframe,
    ZoneState,
)
from xauusd.domain.types import MarketSnapshot, TradePlan

# Schema version. The engine refuses to load a model whose feature schema hash does not
# match, so bumping this invalidates stale models rather than silently mis-scoring.
FEATURE_SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class FeatureVector:
    # --- higher-timeframe context ---
    bias_mn: int = 0
    bias_w: int = 0
    bias_d: int = 0
    bias_h4: int = 0
    bias_h1: int = 0
    bias_m15: int = 0
    htf_alignment: float = 0.0  # -1..1, agreement of MN/W/D/H4 with the direction
    htf_conflict: int = 0  # 1 if any HTF bias opposes the direction

    # --- structure ---
    has_mss: int = 0
    mss_displacement_atr: float = 0.0
    has_bos: int = 0
    bos_displacement_atr: float = 0.0
    structure_age_bars: float = 0.0

    # --- liquidity ---
    sweep_quality: float = 0.0
    sweep_penetration_atr: float = 0.0
    sweep_rejection: float = 0.0
    sweep_displacement_atr: float = 0.0
    sweep_pool_strength: float = 0.0
    sweep_recent: int = 0
    target_liquidity_count: int = 0
    target_liquidity_rr: float = 0.0
    opposing_liquidity_count: int = 0
    opposing_liquidity_distance_r: float = 0.0

    # --- zones ---
    fvg_quality: float = 0.0
    fvg_size_atr: float = 0.0
    fvg_displacement_atr: float = 0.0
    fvg_unmitigated: int = 0
    ob_quality: float = 0.0
    ob_displacement_atr: float = 0.0
    ob_fresh: int = 0
    ob_has_fvg: int = 0
    zone_confluence: int = 0

    # --- location ---
    premium_discount_position: float = 0.5
    correct_side_of_range: int = 0
    distance_to_equilibrium_atr: float = 0.0
    sr_confluence_count: int = 0
    sr_confluence_importance: float = 0.0
    sr_blocking_target: int = 0

    # --- macro ---
    macro_bias: int = 0
    macro_known: int = 0
    macro_aligned: int = 0
    dxy_change_5d: float = 0.0
    dxy_implication_aligned: int = 0
    real10y: float = 0.0
    yields_implication_aligned: int = 0

    # --- news / calendar ---
    news_risk: int = 1  # 0 LOW .. 3 EXTREME
    news_blackout: int = 0
    news_aligned: int = 0
    minutes_to_next_event: float = 9999.0
    news_stale: int = 0

    # --- session / regime / volatility ---
    session_id: int = 0
    killzone: int = 0
    hour_utc: int = 0
    day_of_week: int = 0
    minutes_into_session: int = 0
    regime_id: int = 0
    regime_aligned: int = 0
    vol_regime: int = 1
    atr_h1: float = 0.0
    atr_percentile: float = 0.5
    spread_points: float = 0.0
    spread_ratio: float = 1.0

    # --- trade geometry ---
    direction: int = 0
    rr: float = 0.0
    risk_distance_atr: float = 0.0
    entry_distance_atr: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def names(self) -> list[str]:
        return sorted(asdict(self))

    def vector(self) -> list[float]:
        d = asdict(self)
        return [float(d[k]) for k in sorted(d)]


_SESSION_ID = {
    Session.OFF: 0,
    Session.ASIA: 1,
    Session.LONDON: 2,
    Session.NEW_YORK: 3,
    Session.OVERLAP: 4,
}
_REGIME_ID = {
    Regime.ABNORMAL: 0,
    Regime.RANGE: 1,
    Regime.MODERATE_BULL: 2,
    Regime.STRONG_BULL: 3,
    Regime.MODERATE_BEAR: 4,
    Regime.STRONG_BEAR: 5,
    Regime.NEWS_DRIVEN: 6,
}
_VOL_ID = {"LOW": 0, "NORMAL": 1, "HIGH": 2, "EXTREME": 3}


def extract(snap: MarketSnapshot, plan: TradePlan) -> FeatureVector:
    """Build the feature vector for one candidate against one snapshot."""
    f = FeatureVector()
    d = plan.direction
    atr = snap.volatility.atr_m15 or snap.volatility.atr_h1 or 1.0
    if not atr or atr != atr:  # NaN guard
        atr = 1.0

    # --- HTF ---
    for tf, attr in (
        (Timeframe.MN1, "bias_mn"),
        (Timeframe.W1, "bias_w"),
        (Timeframe.D1, "bias_d"),
        (Timeframe.H4, "bias_h4"),
        (Timeframe.H1, "bias_h1"),
        (Timeframe.M15, "bias_m15"),
    ):
        setattr(f, attr, snap.bias(tf).sign)

    htf = [snap.bias(tf) for tf in (Timeframe.MN1, Timeframe.W1, Timeframe.D1, Timeframe.H4)]
    known = [b for b in htf if b is not Bias.NEUTRAL]
    if known:
        f.htf_alignment = sum(1 if b.agrees_with(d) else -1 for b in known) / len(known)
    f.htf_conflict = int(any(b.conflicts_with(d) for b in htf))

    # --- structure on the setup timeframe ---
    st = snap.structures.get(plan.setup_timeframe) or snap.structures.get(Timeframe.M15)
    if st:
        if st.last_mss and st.last_mss.direction is d:
            f.has_mss = 1
            f.mss_displacement_atr = st.last_mss.displacement_atr
        if st.last_bos and st.last_bos.direction is d:
            f.has_bos = 1
            f.bos_displacement_atr = st.last_bos.displacement_atr
        if st.last_event:
            f.structure_age_bars = max(
                0.0,
                (snap.ts - st.last_event.ts).total_seconds() / max(plan.setup_timeframe.seconds, 1),
            )

    # --- liquidity ---
    sweeps = [s for s in snap.sweeps if s.direction is d]
    if sweeps:
        best = max(sweeps, key=lambda s: s.quality)
        f.sweep_quality = best.quality
        f.sweep_penetration_atr = best.penetration_atr
        f.sweep_rejection = best.rejection_ratio
        f.sweep_displacement_atr = best.displacement_after_atr
        f.sweep_pool_strength = best.pool.strength
        age_bars = (snap.ts - best.ts).total_seconds() / max(plan.setup_timeframe.seconds, 1)
        f.sweep_recent = int(age_bars <= 12)

    targets = snap.resting_liquidity(buyside=d is Direction.LONG)
    ahead = [
        p
        for p in targets
        if ((p.price > plan.entry) if d is Direction.LONG else (p.price < plan.entry))
    ]
    f.target_liquidity_count = len(ahead)
    if ahead and plan.risk_distance > 0:
        furthest = max(ahead, key=lambda p: abs(p.price - plan.entry))
        f.target_liquidity_rr = abs(furthest.price - plan.entry) / plan.risk_distance

    against = snap.resting_liquidity(buyside=d is Direction.SHORT)
    behind = [
        p
        for p in against
        if ((p.price < plan.entry) if d is Direction.LONG else (p.price > plan.entry))
    ]
    f.opposing_liquidity_count = len(behind)
    if behind and plan.risk_distance > 0:
        nearest = min(behind, key=lambda p: abs(p.price - plan.entry))
        f.opposing_liquidity_distance_r = abs(nearest.price - plan.entry) / plan.risk_distance

    # --- zones ---
    zone_lo = plan.entry_zone_bottom if plan.entry_zone_bottom is not None else plan.entry
    zone_hi = plan.entry_zone_top if plan.entry_zone_top is not None else plan.entry
    fvgs = [z for z in snap.fvgs if z.direction is d and z.bottom <= zone_hi and z.top >= zone_lo]
    if fvgs:
        best_f = max(fvgs, key=lambda z: z.displacement_atr)
        f.fvg_size_atr = best_f.size_atr
        f.fvg_displacement_atr = best_f.displacement_atr
        f.fvg_unmitigated = int(best_f.state is FVGState.UNMITIGATED)
        f.fvg_quality = float(plan.evidence.get("fvg_quality", 0.0))
    obs = [
        z
        for z in snap.order_blocks
        if z.direction is d and z.bottom <= zone_hi and z.top >= zone_lo
    ]
    if obs:
        best_o = max(obs, key=lambda z: z.displacement_atr)
        f.ob_displacement_atr = best_o.displacement_atr
        f.ob_fresh = int(best_o.state is ZoneState.FRESH)
        f.ob_has_fvg = int(best_o.has_fvg)
        f.ob_quality = float(plan.evidence.get("ob_quality", 0.0))
    f.zone_confluence = int(bool(fvgs) and bool(obs))

    # --- location ---
    pos, _ = premium_discount(snap.dealing_range, plan.entry)
    f.premium_discount_position = pos
    f.correct_side_of_range = int(pos < 0.5 if d is Direction.LONG else pos > 0.5)
    if snap.dealing_range:
        f.distance_to_equilibrium_atr = abs(plan.entry - snap.dealing_range.equilibrium) / atr

    tol = 0.5 * atr
    near = [lv for lv in snap.sr_levels if abs(lv.price - plan.entry) <= tol]
    f.sr_confluence_count = len(near)
    f.sr_confluence_importance = max((lv.importance for lv in near), default=0.0)
    lo, hi = (
        (plan.entry, plan.final_target.price)
        if d is Direction.LONG
        else (plan.final_target.price, plan.entry)
    )
    f.sr_blocking_target = int(
        any(lo < lv.price < hi and lv.importance >= 0.45 for lv in snap.sr_levels)
    )

    # --- macro ---
    m = snap.macro
    f.macro_bias = m.bias.score
    f.macro_known = int(m.bias.is_known)
    f.macro_aligned = int(m.bias.score * d.sign > 0)
    f.dxy_change_5d = m.dxy_change_5d or 0.0
    f.dxy_implication_aligned = int(m.dxy_implication.agrees_with(d))
    f.real10y = m.real10y or 0.0
    f.yields_implication_aligned = int(m.yields_implication.agrees_with(d))

    # --- news ---
    n = snap.news
    f.news_risk = n.risk.level
    f.news_blackout = int(n.blackout)
    f.news_aligned = int(n.directional_hint.agrees_with(d))
    f.minutes_to_next_event = (
        n.minutes_to_next_event if n.minutes_to_next_event is not None else 9999.0
    )
    f.news_stale = int(n.is_stale)

    # --- session / regime ---
    s = snap.session
    f.session_id = _SESSION_ID.get(s.session, 0)
    f.killzone = int(str(s.killzone) != "NONE")
    f.hour_utc = snap.ts.hour
    f.day_of_week = s.day_of_week
    f.minutes_into_session = s.minutes_into_session
    f.regime_id = _REGIME_ID.get(snap.regime, 0)
    trend_dir = (
        1
        if snap.regime in (Regime.STRONG_BULL, Regime.MODERATE_BULL)
        else -1
        if snap.regime in (Regime.STRONG_BEAR, Regime.MODERATE_BEAR)
        else 0
    )
    f.regime_aligned = int(trend_dir == d.sign or trend_dir == 0)
    f.vol_regime = _VOL_ID.get(str(snap.volatility.vol_regime), 1)
    f.atr_h1 = snap.volatility.atr_h1 if snap.volatility.atr_h1 == snap.volatility.atr_h1 else 0.0
    f.atr_percentile = (
        snap.volatility.atr_h1_percentile
        if snap.volatility.atr_h1_percentile == snap.volatility.atr_h1_percentile
        else 0.5
    )
    f.spread_points = snap.volatility.spread_points
    f.spread_ratio = snap.volatility.spread_ratio

    # --- geometry ---
    f.direction = d.sign
    f.rr = plan.rr
    f.risk_distance_atr = plan.risk_distance / atr
    f.entry_distance_atr = abs(plan.entry - snap.quote.mid) / atr
    return f


def schema_hash() -> str:
    """Hash of the feature names. A model trained on a different schema is refused."""
    import hashlib

    names = ",".join(FeatureVector().names())
    return hashlib.blake2s(f"{FEATURE_SCHEMA_VERSION}|{names}".encode(), digest_size=8).hexdigest()
