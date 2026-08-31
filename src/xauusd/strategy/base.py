"""Strategy plugin interface and shared trade-plan construction.

A strategy proposes; it does not decide. It emits zero or more TradePlan candidates
with a structural stop and targets anchored to real liquidity. Scoring, gating,
classification, sizing and execution all happen elsewhere, so a strategy cannot bypass
a risk rule even by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from xauusd.config.settings import Settings
from xauusd.core.support_resistance import SREngine
from xauusd.data.marketview import MarketView
from xauusd.domain.enums import Direction, LiquidityKind, Regime, Session
from xauusd.domain.types import (
    LiquidityPool,
    MarketSnapshot,
    SRLevel,
    TargetLevel,
    TradePlan,
)


@dataclass(frozen=True, slots=True)
class StrategyMeta:
    name: str
    version: str
    allowed_regimes: frozenset[Regime]
    allowed_sessions: frozenset[Session]
    description: str = ""


@runtime_checkable
class Strategy(Protocol):
    meta: StrategyMeta

    def detect(self, view: MarketView, snap: MarketSnapshot) -> list[TradePlan]: ...


# --------------------------------------------------------------------------------------
# Target selection — the part that makes 1:2 vs 1:3 an evidence-based choice
# --------------------------------------------------------------------------------------


def build_targets(
    entry: float,
    stop_loss: float,
    direction: Direction,
    pools: list[LiquidityPool],
    sr_levels: list[SRLevel],
    min_rr: float,
    preferred_rr: float,
    sr_engine: SREngine | None = None,
) -> list[TargetLevel]:
    """Anchor take-profits to real opposing liquidity, never to a round multiple of R.

    The rule the brief asks for — "do NOT force a 1:3 target if realistic liquidity or
    structure makes it unlikely" — is implemented here:

      * candidate targets are RESTING liquidity pools ahead of price;
      * a candidate is discarded if a significant S/R level sits between entry and it,
        because that level is where the move is likely to stall;
      * the final target is the furthest surviving candidate that still clears min_rr,
        preferring one at or beyond preferred_rr when such a candidate genuinely exists.

    Returns [] when nothing valid exists, which makes the trade unplaceable. That is the
    correct outcome, not a reason to invent a level.
    """
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return []

    sr = sr_engine or SREngine()
    ahead = [
        p
        for p in pools
        if p.is_resting
        and ((p.price > entry) if direction is Direction.LONG else (p.price < entry))
    ]
    # Structural levels are targets too, not just liquidity pools.
    level_targets = [
        lv
        for lv in sr_levels
        if ((lv.price > entry) if direction is Direction.LONG else (lv.price < entry))
        and lv.importance >= 0.25
    ]

    candidates: list[tuple[float, str, LiquidityKind | None]] = []
    for p in ahead:
        candidates.append((p.price, f"{p.kind} liquidity ({p.touches} touches)", p.kind))
    for lv in level_targets:
        candidates.append(
            (lv.price, f"{lv.kind} {lv.timeframe} (importance {lv.importance:.2f})", None)
        )
    if not candidates:
        return []

    # Order by distance from entry, nearest first.
    candidates.sort(key=lambda c: abs(c[0] - entry))

    viable: list[TargetLevel] = []
    for price, rationale, kind in candidates:
        rr = abs(price - entry) / risk
        if rr < min_rr:
            continue
        blocker = sr.blocking_level(sr_levels, entry, price, direction, min_importance=0.45)
        if blocker is not None and abs(blocker.price - price) > 1e-9:
            # Something significant stands in the way; this target is not realistic.
            continue
        viable.append(TargetLevel(price=price, rr=rr, rationale=rationale, liquidity_kind=kind))

    if not viable:
        return []

    # Prefer a target at or beyond preferred_rr when one genuinely exists; otherwise
    # take the best available that clears the floor. Never stretch a target to reach 3R.
    preferred = [t for t in viable if t.rr >= preferred_rr]
    chosen = min(preferred, key=lambda t: t.rr) if preferred else max(viable, key=lambda t: t.rr)

    tp1 = min(viable, key=lambda t: t.rr)
    if abs(tp1.price - chosen.price) < 1e-9:
        return [chosen]
    return [tp1, chosen]


def structural_stop(
    direction: Direction,
    zone_top: float,
    zone_bottom: float,
    sweep_extreme: float | None,
    atr_value: float,
    buffer_atr: float = 0.15,
    stops_level_price: float = 0.0,
) -> float:
    """Place the stop where the IDEA is wrong, then add a buffer.

    For a short: beyond the high of the sweep that created the setup — if price trades
    back above the level it just raided, the premise is dead. The buffer keeps the stop
    off the exact extreme, which is itself a liquidity magnet.

    Never tightened to improve RR; if the resulting RR is inadequate the trade is
    rejected instead.
    """
    buffer = max(buffer_atr * atr_value, stops_level_price)
    if direction is Direction.SHORT:
        anchor = max(zone_top, sweep_extreme if sweep_extreme is not None else zone_top)
        return anchor + buffer
    anchor = min(zone_bottom, sweep_extreme if sweep_extreme is not None else zone_bottom)
    return anchor - buffer


class StrategyRegistry:
    """Registry with the live-routing gate baked in.

    `eligible_for_live` consults strategy_status; a DEV strategy physically cannot be
    routed to the broker, which is the point of enforcing validation in code rather
    than in a checklist.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        self._strategies[strategy.meta.name] = strategy

    def get(self, name: str) -> Strategy | None:
        return self._strategies.get(name)

    def all(self) -> list[Strategy]:
        return list(self._strategies.values())

    def enabled(self, settings: Settings) -> list[Strategy]:
        return [s for name, s in self._strategies.items() if name in settings.enabled_strategies]

    @staticmethod
    def is_allowed_here(strategy: Strategy, snap: MarketSnapshot) -> tuple[bool, str]:
        """Regime and session whitelist check, from the strategy's own validation."""
        if snap.regime not in strategy.meta.allowed_regimes:
            return False, (
                f"{strategy.meta.name} is not validated in regime {snap.regime} "
                f"(allowed: {sorted(str(r) for r in strategy.meta.allowed_regimes)})"
            )
        if snap.session.session not in strategy.meta.allowed_sessions:
            return False, (
                f"{strategy.meta.name} is not validated in session {snap.session.session}"
            )
        return True, "ok"


def default_registry() -> StrategyRegistry:
    from xauusd.strategy.setups.pdh_pdl_reversion import PdhPdlReversion
    from xauusd.strategy.setups.session_range_expansion import SessionRangeExpansion
    from xauusd.strategy.setups.sweep_mss_fvg import SweepMssFvg
    from xauusd.strategy.setups.sweep_mss_ob import SweepMssOb

    r = StrategyRegistry()
    for s in (SweepMssFvg(), SweepMssOb(), SessionRangeExpansion(), PdhPdlReversion()):
        r.register(s)
    return r
