"""Macro regime for gold: real yields, nominal yields, the dollar and the curve.

Ordering of importance, which the weights reflect:
  1. 10y REAL yield (DFII10) — the opportunity cost of holding a non-yielding asset.
     This is the single most important macro driver of gold and is weighted accordingly.
  2. The dollar.
  3. Nominal yields and inflation expectations.
  4. The curve, as a risk-off proxy.

Fundamentals INFLUENCE bias. They never override validated price action, and this
module returns a bias plus a confidence, never a trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from xauusd.domain.enums import Bias, MacroBias
from xauusd.domain.types import MacroState
from xauusd.intelligence.dxy import DxyState

# FRED series the system consumes.
SERIES = {
    "DGS2": "2-year Treasury constant maturity",
    "DGS10": "10-year Treasury constant maturity",
    "DFII10": "10-year TIPS (real) yield",
    "DFII5": "5-year TIPS (real) yield",
    "T10YIE": "10-year breakeven inflation",
    "T10Y2Y": "10y minus 2y spread",
    "DFF": "Effective federal funds rate",
    "DTWEXBGS": "Broad trade-weighted dollar index",
}


def _trend_of(points: Sequence[tuple[datetime, float]], min_change: float) -> tuple[Bias, float]:
    """(direction, change) over the supplied point-in-time window."""
    if len(points) < 2:
        return Bias.NEUTRAL, 0.0
    change = points[-1][1] - points[0][1]
    if change > min_change:
        return Bias.BULLISH, change
    if change < -min_change:
        return Bias.BEARISH, change
    return Bias.NEUTRAL, change


@dataclass(frozen=True, slots=True)
class MacroInputs:
    """Point-in-time macro series. Every value must already be release-filtered."""

    real10y: list[tuple[datetime, float]]
    nominal10y: list[tuple[datetime, float]]
    nominal2y: list[tuple[datetime, float]]
    breakeven10y: list[tuple[datetime, float]]
    curve10y2y: list[tuple[datetime, float]]
    as_of: datetime
    dxy: DxyState | None = None

    @property
    def latest_real10y(self) -> float | None:
        return self.real10y[-1][1] if self.real10y else None

    @property
    def age_days(self) -> float:
        stamps = [p[0] for p in (self.real10y or self.nominal10y or [])]
        return (self.as_of - max(stamps)).days if stamps else 1e9


class MacroEngine:
    """Weighted, explainable macro classification."""

    # Weights sum to 1.0. Real yields dominate on purpose.
    W_REAL_YIELD = 0.40
    W_DXY = 0.25
    W_NOMINAL = 0.15
    W_BREAKEVEN = 0.12
    W_CURVE = 0.08

    def __init__(self, max_age_days: int = 3) -> None:
        self.max_age_days = max_age_days

    def classify(self, inputs: MacroInputs) -> tuple[MacroState, dict[str, float]]:
        stale = inputs.age_days > self.max_age_days
        contributions: dict[str, float] = {}

        # Real yields: RISING real yields are BEARISH gold. 5bp is a meaningful move.
        ry_trend, ry_change = _trend_of(inputs.real10y, 0.05)
        contributions["real10y"] = -ry_trend.sign * self.W_REAL_YIELD

        # Dollar: a stronger dollar is bearish gold.
        dxy_trend = inputs.dxy.trend if inputs.dxy else Bias.NEUTRAL
        contributions["dxy"] = -dxy_trend.sign * self.W_DXY

        n10_trend, _ = _trend_of(inputs.nominal10y, 0.08)
        contributions["nominal10y"] = -n10_trend.sign * self.W_NOMINAL

        # Rising inflation expectations are supportive of gold.
        be_trend, _ = _trend_of(inputs.breakeven10y, 0.04)
        contributions["breakeven10y"] = be_trend.sign * self.W_BREAKEVEN

        # A flattening/inverting curve is a risk-off signal, mildly gold-supportive.
        cv_trend, _ = _trend_of(inputs.curve10y2y, 0.06)
        contributions["curve"] = -cv_trend.sign * self.W_CURVE

        score = sum(contributions.values())
        if stale or not inputs.real10y:
            bias = MacroBias.UNKNOWN
        elif score >= 0.45:
            bias = MacroBias.STRONGLY_BULLISH
        elif score >= 0.15:
            bias = MacroBias.BULLISH
        elif score <= -0.45:
            bias = MacroBias.STRONGLY_BEARISH
        elif score <= -0.15:
            bias = MacroBias.BEARISH
        else:
            bias = MacroBias.NEUTRAL

        d = inputs.dxy
        state = MacroState(
            bias=bias,
            dxy_level=d.level if d else None,
            dxy_change_1d=d.change_1 if d else None,
            dxy_change_5d=d.change_5 if d else None,
            dxy_trend=dxy_trend,
            us10y=inputs.nominal10y[-1][1] if inputs.nominal10y else None,
            us2y=inputs.nominal2y[-1][1] if inputs.nominal2y else None,
            real10y=inputs.latest_real10y,
            real10y_change_5d=ry_change,
            breakeven10y=inputs.breakeven10y[-1][1] if inputs.breakeven10y else None,
            yields_trend=ry_trend,
            curve_10y2y=inputs.curve10y2y[-1][1] if inputs.curve10y2y else None,
            as_of=inputs.as_of,
            is_stale=stale,
            detail={
                "score": round(score, 4),
                "contributions": {k: round(v, 4) for k, v in contributions.items()},
                "age_days": inputs.age_days,
            },
        )
        return state, contributions

    @staticmethod
    def explain(state: MacroState) -> list[str]:
        out: list[str] = []
        if state.bias is MacroBias.UNKNOWN:
            return ["macro data unavailable or stale — treated as unknown, not neutral"]
        if state.real10y is not None:
            direction = (
                "rising"
                if state.yields_trend is Bias.BULLISH
                else "falling"
                if state.yields_trend is Bias.BEARISH
                else "flat"
            )
            out.append(
                f"10y real yield {state.real10y:.2f}% and {direction} "
                f"({'headwind' if direction == 'rising' else 'tailwind' if direction == 'falling' else 'neutral'} for gold)"
            )
        if state.dxy_level is not None:
            out.append(
                f"DXY {state.dxy_level:.2f} {state.dxy_trend} ({state.dxy_implication} for gold)"
            )
        if state.breakeven10y is not None:
            out.append(f"10y breakeven inflation {state.breakeven10y:.2f}%")
        return out


def build_macro_inputs(repo, as_of: datetime, dxy: DxyState | None = None, lookback_days: int = 30):  # type: ignore[no-untyped-def]
    """Assemble point-in-time macro inputs from the repository.

    Every read goes through `series_as_of`, which filters on release_ts, so a value
    revised months later can never appear in a backtest of today.
    """

    def get(series_id: str) -> list[tuple[datetime, float]]:
        return repo.series_as_of(series_id, as_of, lookback_days)

    return MacroInputs(
        real10y=get("DFII10"),
        nominal10y=get("DGS10"),
        nominal2y=get("DGS2"),
        breakeven10y=get("T10YIE"),
        curve10y2y=get("T10Y2Y"),
        as_of=as_of,
        dxy=dxy,
    )
