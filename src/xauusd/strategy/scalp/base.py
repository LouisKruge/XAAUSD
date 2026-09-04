"""The scalp model contract: what a model produces, and what happens to it after.

Each model is an independent hypothesis about a short-lived inefficiency. It answers
one question — "is my pattern present right now, and where would the stop and target
go?" — and nothing else. It does not decide whether to trade.

That separation is the whole architecture. A model that also judged risk, cost and
correlation would be twelve copies of that judgement, each free to drift. Instead every
model emits a `ScalpSignal`, and one pipeline scores it, prices it, gates it and sizes
it. Adding a thirteenth model cannot weaken the thirteenth trade.

A signal is a *candidate*. Between here and the broker sit the scalp score, the cost
and net-expectancy gates, the existing risk gate with its unchanged caps, and the
correlation budget. Any of them can refuse it, and most will.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from xauusd.core.micro_structure import MicroSnapshot
from xauusd.domain.enums import Direction, Regime, Timeframe
from xauusd.domain.types import MarketSnapshot, TargetLevel, TradePlan


@dataclass(frozen=True, slots=True)
class ScalpFactors:
    """The soft evidence, each normalised to 0..1. The scorer weights these.

    Normalising in the model rather than the scorer is deliberate: only the model knows
    what "strong momentum" means for its own pattern, and only the scorer should know
    how much that is worth relative to everything else.
    """

    market_structure: float = 0.0
    liquidity: float = 0.0
    momentum: float = 0.0
    entry_location: float = 0.0
    volatility: float = 0.0
    session: float = 0.0
    dxy: float = 0.0
    news: float = 0.0
    htf_context: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "market_structure": self.market_structure,
            "liquidity": self.liquidity,
            "momentum": self.momentum,
            "entry_location": self.entry_location,
            "volatility": self.volatility,
            "session": self.session,
            "dxy": self.dxy,
            "news": self.news,
            "htf_context": self.htf_context,
        }


@dataclass(frozen=True, slots=True)
class ScalpSignal:
    """One candidate from one model. Not a decision."""

    model: str
    version: str
    direction: Direction
    entry: float
    stop_loss: float
    target: float
    ts: datetime
    factors: ScalpFactors
    evidence: dict[str, object] = field(default_factory=dict)
    # What the trade is premised on, so the correlation gate can tell two signals about
    # the same event apart from two signals about different ones.
    liquidity_ref: float | None = None
    zone_top: float | None = None
    zone_bottom: float | None = None

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def gross_rr(self) -> float:
        risk = self.stop_distance
        return abs(self.target - self.entry) / risk if risk > 0 else 0.0

    def to_plan(self, symbol: str) -> TradePlan:
        """Convert to the plan type the risk and execution path already understands.

        Reusing TradePlan rather than inventing a parallel type is what lets a scalp go
        through the same RiskGate, the same sizing cross-check and the same execution
        manager as an A/A+ trade. There is one path to the broker.
        """
        return TradePlan(
            strategy=self.model,
            strategy_version=self.version,
            direction=self.direction,
            entry=self.entry,
            stop_loss=self.stop_loss,
            targets=(
                TargetLevel(
                    price=self.target,
                    rr=self.gross_rr,
                    rationale=str(self.evidence.get("target_rationale", "structural target")),
                ),
            ),
            ts=self.ts,
            setup_timeframe=Timeframe.M5,
            symbol=symbol,
            entry_zone_top=self.zone_top,
            entry_zone_bottom=self.zone_bottom,
            invalidation=str(self.evidence.get("invalidation", "")),
            evidence=dict(self.evidence),
        )


@dataclass(frozen=True, slots=True)
class ScalpModelMeta:
    name: str
    version: str
    description: str
    # Regimes the model is *hypothesised* to work in. Replaced by validation output;
    # until then it is a starting point, and the model ships disabled regardless.
    hypothesised_regimes: frozenset[Regime]


@runtime_checkable
class ScalpModel(Protocol):
    meta: ScalpModelMeta

    def detect(self, micro: MicroSnapshot, snap: MarketSnapshot) -> list[ScalpSignal]: ...


class ScalpRegistry:
    """The enabled set, and nothing more.

    A model absent from `enabled_models` cannot produce a signal, which is how a model
    that fails validation is removed from live behaviour without deleting its code or
    its measurement history.
    """

    def __init__(self) -> None:
        self._models: dict[str, ScalpModel] = {}

    def register(self, model: ScalpModel) -> None:
        if model.meta.name in self._models:
            raise ValueError(f"duplicate scalp model: {model.meta.name}")
        self._models[model.meta.name] = model

    def all(self) -> list[ScalpModel]:
        return list(self._models.values())

    def enabled(self, names: list[str]) -> list[ScalpModel]:
        return [m for name, m in self._models.items() if name in names]

    def __len__(self) -> int:
        return len(self._models)


# -- shared helpers ------------------------------------------------------------------


def clamp01(value: float) -> float:
    """Normalise, and never let a NaN become a score.

    A NaN factor propagating into the total would produce a NaN score, which compares
    false against every threshold and silently rejects the signal for no stated reason.
    Warm-up must reject a trade loudly, not invisibly.
    """
    if value != value:
        return 0.0
    return max(0.0, min(1.0, value))


def structural_target(
    entry: float,
    stop: float,
    direction: Direction,
    target_rr: float,
    obstacles: list[float],
) -> tuple[float, str]:
    """Target at the requested RR, pulled in to the nearest obstacle in the way.

    Reaching *through* resting liquidity to hit a round number is how a 1:1.5 target
    becomes a 1:0.8 fill. So the nearest opposing level short of the RR target wins,
    and the caller decides whether what remains still clears its costs.
    """
    risk = abs(entry - stop)
    ideal = entry + risk * target_rr if direction is Direction.LONG else entry - risk * target_rr

    ahead = [
        o
        for o in obstacles
        if (direction is Direction.LONG and entry < o <= ideal)
        or (direction is Direction.SHORT and ideal <= o < entry)
    ]
    if not ahead:
        return ideal, f"{target_rr:.2f}R, no obstacle in the way"
    nearest = min(ahead) if direction is Direction.LONG else max(ahead)
    return nearest, f"nearest opposing level at {nearest:.2f}, short of the {target_rr:.2f}R target"
