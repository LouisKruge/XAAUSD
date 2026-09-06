"""The Market Regime Controller — the layer that decides WHICH ENGINE MAY TRADE.

Specification §31. It sits above both engines and answers one question per instant:

    SCALPING ONLY | INTRADAY ONLY | BOTH | NEITHER

This is deliberately a separate layer rather than another gate inside each engine. Two
engines each deciding for themselves whether conditions suit them is two opinions that
can disagree, and the disagreement is invisible — the classic shape of defect this
project keeps finding (FINDINGS 38, 40, BUG-002). One controller, consulted by both,
means the answer is recorded once and the journal can show it.

**It can only ever REMOVE permission.** It never grants a trade, never overrides the
risk engine, the kill switch or the drawdown guard, and never widens a limit. §31 is
explicit: "The controller should never override hard safety rules." So its output is
consumed as an additional veto, downstream of nothing and upstream of everything.

The regime read itself is not reinvented here: `RegimeEngine` already classifies trend
and volatility from price, and `MarketSnapshot` already carries session, spread and news
state. This module only maps that existing evidence onto engine permissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from xauusd.config.settings import Settings
from xauusd.domain.enums import NewsRisk, Regime, VolRegime
from xauusd.domain.types import MarketSnapshot
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


class EnginePermission(StrEnum):
    """Which engines may open NEW positions. Existing positions are always managed."""

    NEITHER = "NEITHER"
    SCALP_ONLY = "SCALP_ONLY"
    INTRADAY_ONLY = "INTRADAY_ONLY"
    BOTH = "BOTH"

    @property
    def scalp_allowed(self) -> bool:
        return self in (EnginePermission.SCALP_ONLY, EnginePermission.BOTH)

    @property
    def intraday_allowed(self) -> bool:
        return self in (EnginePermission.INTRADAY_ONLY, EnginePermission.BOTH)


@dataclass(frozen=True, slots=True)
class RegimeVerdict:
    """The controller's answer, with its reasoning attached.

    The reasons travel with the verdict rather than being logged and discarded, because
    "the bot is not trading" and "the bot is not trading BECAUSE spread is triple its
    median" are different facts, and only the second is actionable. This project has
    twice paid for a correct decision that could not explain itself (FINDINGS 37, 41).
    """

    permission: EnginePermission
    regime: Regime
    scalp_reasons: tuple[str, ...] = ()
    intraday_reasons: tuple[str, ...] = ()

    @property
    def scalp_allowed(self) -> bool:
        return self.permission.scalp_allowed

    @property
    def intraday_allowed(self) -> bool:
        return self.permission.intraday_allowed

    def why_not(self, engine: str) -> str:
        reasons = self.scalp_reasons if engine == "scalp" else self.intraday_reasons
        return "; ".join(reasons) if reasons else ""

    def as_dict(self) -> dict[str, object]:
        return {
            "permission": str(self.permission),
            "regime": str(self.regime),
            "scalp_allowed": self.scalp_allowed,
            "intraday_allowed": self.intraday_allowed,
            "scalp_blocked_by": list(self.scalp_reasons),
            "intraday_blocked_by": list(self.intraday_reasons),
        }


# Regimes in which a directional trend engine has something to work with. RANGE is
# excluded on purpose: §20 says a range must not be forced into a trend trade.
TRENDING = frozenset(
    {Regime.STRONG_BULL, Regime.MODERATE_BULL, Regime.MODERATE_BEAR, Regime.STRONG_BEAR}
)


class MarketRegimeController:
    """Maps market conditions onto engine permissions. Removes permission only."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def evaluate(self, snap: MarketSnapshot) -> RegimeVerdict:
        cfg = self.settings.regime_controller
        scalp_blocks: list[str] = []
        intraday_blocks: list[str] = []

        # --- conditions that stop BOTH engines -------------------------------------
        # §32: extreme news and abnormal execution conditions halt everything. Note the
        # asymmetry with §32's later paragraph — intraday MAY trade a validated
        # post-news expansion, but never during the initial chaotic spike, and "during"
        # is exactly what a blackout flag marks.
        if snap.news.blackout:
            reason = f"news blackout: {snap.news.blackout_reason or 'unspecified'}"
            scalp_blocks.append(reason)
            intraday_blocks.append(reason)

        vol = snap.volatility
        # `spread_ratio` is the snapshot's own definition; recomputing it here would be
        # a second answer free to drift from the one every other gate uses.
        if vol.spread_points > 0 and vol.spread_median_points > 0:
            ratio = vol.spread_ratio
            if ratio > cfg.abnormal_spread_ratio:
                reason = (
                    f"spread {vol.spread_points:.0f}pts is {ratio:.1f}x its median "
                    f"{vol.spread_median_points:.0f} (limit {cfg.abnormal_spread_ratio:.1f}x)"
                )
                scalp_blocks.append(reason)
                intraday_blocks.append(reason)

        # --- scalp-specific ---------------------------------------------------------
        # §31: high volatility and choppy conditions can suit scalping while ruling out
        # a trend trade, so a DEAD market is the scalp's problem, not a violent one.
        # A snapback needs something to snap back FROM.
        if vol.vol_regime is VolRegime.LOW:
            scalp_blocks.append("volatility LOW: no dislocation to snap back from")

        if str(snap.news.risk) in {str(NewsRisk.HIGH), str(NewsRisk.EXTREME)}:
            scalp_blocks.append(f"news risk {snap.news.risk}: execution unreliable")

        # --- intraday-specific -------------------------------------------------------
        # §20: a range is not a trend. §21: conflicting timeframes mean no trade.
        if snap.regime not in TRENDING:
            intraday_blocks.append(f"regime {snap.regime} is not directional")

        if vol.vol_regime is VolRegime.EXTREME:
            intraday_blocks.append("volatility EXTREME: structure unreliable for a pullback entry")

        # §21, stated as a hard requirement: H4 and H1 must agree.
        from xauusd.domain.enums import Timeframe

        h4, h1 = snap.bias(Timeframe.H4), snap.bias(Timeframe.H1)
        if h4.sign != 0 and h1.sign != 0 and h4.sign != h1.sign:
            intraday_blocks.append(f"H4 {h4} conflicts with H1 {h1}")

        permission = self._combine(not scalp_blocks, not intraday_blocks)
        verdict = RegimeVerdict(
            permission=permission,
            regime=snap.regime,
            scalp_reasons=tuple(scalp_blocks),
            intraday_reasons=tuple(intraday_blocks),
        )
        if permission is not EnginePermission.BOTH:
            log.debug("regime_controller", **verdict.as_dict())
        return verdict

    @staticmethod
    def _combine(scalp_ok: bool, intraday_ok: bool) -> EnginePermission:
        if scalp_ok and intraday_ok:
            return EnginePermission.BOTH
        if scalp_ok:
            return EnginePermission.SCALP_ONLY
        if intraday_ok:
            return EnginePermission.INTRADAY_ONLY
        return EnginePermission.NEITHER
