"""Hard gates: the mandatory pre-trade checklist.

Every gate is a veto. There is no weighting, no partial credit and no "mostly passed".
If any mandatory gate fails, the decision is NO_TRADE.

Two design points that matter:

1. **Every gate is evaluated and recorded**, even after one has already failed, so the
   decision journal can answer "what ELSE would have blocked this?" rather than only
   naming the first problem. Cheap gates run first so the expensive ones can be skipped
   in the hot path when configured to, but the trace always names what was checked.

2. **Gates read live state, not values computed earlier in the cycle.** Anything that
   could have changed between analysis and execution is re-read at the gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from xauusd.config.settings import Settings
from xauusd.core.sessions import SessionEngine
from xauusd.domain.enums import (
    Direction,
    NewsRisk,
    Regime,
    ValidationStatus,
)
from xauusd.domain.types import (
    AccountState,
    GateResult,
    MarketSnapshot,
    SymbolSpec,
    TradePlan,
)
from xauusd.execution.broker import BrokerHealth


@dataclass(slots=True)
class GateContext:
    """Everything the gates need. Assembled fresh for each evaluation."""

    settings: Settings
    snapshot: MarketSnapshot
    plan: TradePlan | None = None
    spec: SymbolSpec | None = None
    account: AccountState | None = None
    health: BrokerHealth | None = None
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    quote_age_seconds: float = 0.0
    bar_age_seconds: float = 0.0
    open_positions: int = 0
    open_risk_pct: float = 0.0
    trades_today: int = 0
    daily_drawdown_pct: float = 0.0
    weekly_drawdown_pct: float = 0.0
    monthly_drawdown_pct: float = 0.0
    daily_locked: bool = False
    weekly_locked: bool = False
    monthly_locked: bool = False
    duplicate_tag: bool = False
    strategy_status: ValidationStatus = ValidationStatus.DEV
    symbol_resolved: bool = True
    spec_unchanged: bool = True
    session_engine: SessionEngine | None = None


GateFn = Callable[[GateContext], GateResult]


# --------------------------------------------------------------------------------------
# Stage 0 — pre-flight
# --------------------------------------------------------------------------------------


def g_kill_switch(c: GateContext) -> GateResult:
    return GateResult(
        "kill_switch",
        not c.kill_switch_active,
        observed=c.kill_switch_reason or "clear",
        threshold="clear",
        detail=c.kill_switch_reason or "",
    )


def g_broker_connection(c: GateContext) -> GateResult:
    h = c.health
    ok = h is not None and h.is_ok
    return GateResult(
        "broker_connection",
        ok,
        observed=(
            f"connected={h.connected} trade_allowed={h.trade_allowed} expert={h.trade_expert}"
            if h
            else "no health report"
        ),
        threshold="connected and trading enabled",
        detail=h.detail if h else "",
    )


def g_symbol_resolved(c: GateContext) -> GateResult:
    ok = c.symbol_resolved and c.spec is not None and c.spec_unchanged
    detail = ""
    if not c.spec_unchanged:
        detail = "symbol specification changed — every open risk calculation is invalidated"
    return GateResult(
        "symbol_resolved",
        ok,
        observed=c.snapshot.symbol,
        threshold="resolved with an unchanged spec",
        detail=detail,
    )


def g_data_freshness(c: GateContext) -> GateResult:
    e = c.settings.execution
    ok = (
        c.quote_age_seconds <= e.max_quote_age_seconds
        and c.bar_age_seconds <= e.max_bar_age_seconds
    )
    return GateResult(
        "data_freshness",
        ok,
        observed=f"quote {c.quote_age_seconds:.1f}s / bar {c.bar_age_seconds:.0f}s",
        threshold=f"quote <= {e.max_quote_age_seconds}s, bar <= {e.max_bar_age_seconds}s",
    )


# --------------------------------------------------------------------------------------
# Stage 1 — environment
# --------------------------------------------------------------------------------------


def g_spread(c: GateContext) -> GateResult:
    e = c.settings.execution
    v = c.snapshot.volatility
    ok = v.spread_points <= e.max_spread_points and v.spread_ratio <= e.max_spread_ratio
    return GateResult(
        "spread",
        ok,
        observed=f"{v.spread_points:.0f}pts ({v.spread_ratio:.2f}x median)",
        threshold=f"<= {e.max_spread_points}pts and <= {e.max_spread_ratio}x median",
    )


def g_session(c: GateContext) -> GateResult:
    engine = c.session_engine or SessionEngine(c.settings.session)
    ok, why = engine.is_tradable_window(c.snapshot.ts)
    return GateResult(
        "session",
        ok,
        observed=str(c.snapshot.session.session),
        threshold=str([str(s) for s in c.settings.session.allowed_sessions]),
        detail=why,
    )


def g_news_blackout(c: GateContext) -> GateResult:
    n = c.snapshot.news
    return GateResult(
        "news_blackout",
        not n.blackout,
        observed=n.blackout_reason or "clear",
        threshold="no blackout",
        detail=n.blackout_reason or "",
    )


def g_news_risk(c: GateContext) -> GateResult:
    n = c.snapshot.news
    limit = NewsRisk(c.settings.news.news_risk_blackout)
    ok = n.risk.level < limit.level
    return GateResult(
        "news_risk",
        ok,
        observed=str(n.risk),
        threshold=f"< {limit}",
    )


def g_market_regime(c: GateContext) -> GateResult:
    allowed = c.settings.regime.allowed_regimes
    r = c.snapshot.regime
    ok = r.is_tradable and r in allowed
    return GateResult(
        "market_regime",
        ok,
        observed=str(r),
        threshold=str([str(x) for x in allowed]),
        detail="ABNORMAL is never tradable" if r is Regime.ABNORMAL else "",
    )


# --------------------------------------------------------------------------------------
# Stage 2-5 — the setup itself
# --------------------------------------------------------------------------------------


def g_has_candidate(c: GateContext) -> GateResult:
    return GateResult(
        "has_candidate",
        c.plan is not None,
        observed="candidate" if c.plan else "none",
        threshold="a strategy produced a trade plan",
    )


def g_strategy_validated(c: GateContext) -> GateResult:
    """A strategy that has not cleared out-of-sample validation cannot reach the broker."""
    ok = c.strategy_status.live_eligible or not c.settings.mode.is_real_money
    return GateResult(
        "strategy_validated",
        ok,
        observed=str(c.strategy_status),
        threshold="OOS_PASSED or better for live trading",
        detail=(
            "" if ok else "strategy has not passed out-of-sample validation; live routing refused"
        ),
    )


def g_htf_conflict(c: GateContext) -> GateResult:
    if c.plan is None:
        return GateResult("htf_conflict", False, "no plan", "a plan")
    d = c.plan.direction
    conflicts = [
        str(tf)
        for tf, st in c.snapshot.structures.items()
        if tf.rank >= 6 and st.bias.conflicts_with(d)  # D1 and above
    ]
    return GateResult(
        "htf_conflict",
        not conflicts,
        observed=conflicts or "none",
        threshold="no daily-or-higher timeframe opposing the trade",
    )


def g_min_rr(c: GateContext) -> GateResult:
    """The 1:2 floor. Re-checked after price normalisation, never waived."""
    t = c.settings.thresholds
    if c.plan is None:
        return GateResult("min_rr", False, None, t.min_rr)
    rr = c.plan.rr
    return GateResult(
        "min_rr",
        rr >= t.min_rr,
        observed=round(rr, 3),
        threshold=t.min_rr,
        detail=c.plan.final_target.rationale,
    )


def g_stop_validity(c: GateContext) -> GateResult:
    """Stop must be structurally placed AND outside the broker's minimum stop distance."""
    if c.plan is None or c.spec is None:
        return GateResult("stop_validity", False, "no plan or spec", "both present")
    dist = c.plan.risk_distance
    min_dist = c.spec.stops_level_price
    ok = dist > min_dist and dist > 0
    return GateResult(
        "stop_validity",
        ok,
        observed=round(dist, 5),
        threshold=f"> broker stops_level {min_dist:.5f}",
        detail="" if ok else "stop distance inside the broker's minimum — cannot be placed",
    )


def g_premium_discount(c: GateContext) -> GateResult:
    """Longs in discount, shorts in premium. Tolerance permits a small overshoot."""
    if c.plan is None:
        return GateResult("premium_discount", False, "no plan", "a plan")
    dr = c.snapshot.dealing_range
    if dr is None or dr.size <= 0:
        return GateResult(
            "premium_discount",
            True,
            "unknown range",
            "not vetoed",
            detail="dealing range unknown; scoring handles the uncertainty",
        )
    pos = dr.position_of(c.plan.entry)
    ok = pos <= 0.60 if c.plan.direction is Direction.LONG else pos >= 0.40
    return GateResult(
        "premium_discount",
        ok,
        observed=round(pos, 3),
        threshold="<= 0.60 for longs, >= 0.40 for shorts",
        detail=dr.zone_label(c.plan.entry),
    )


# --------------------------------------------------------------------------------------
# Stage 9 — risk
# --------------------------------------------------------------------------------------


def g_daily_drawdown(c: GateContext) -> GateResult:
    lim = c.settings.risk.max_daily_drawdown_pct
    ok = not c.daily_locked and c.daily_drawdown_pct < lim
    return GateResult(
        "daily_drawdown",
        ok,
        observed=round(c.daily_drawdown_pct, 5),
        threshold=lim,
        detail="locked out" if c.daily_locked else "",
    )


def g_weekly_drawdown(c: GateContext) -> GateResult:
    lim = c.settings.risk.max_weekly_drawdown_pct
    ok = not c.weekly_locked and c.weekly_drawdown_pct < lim
    return GateResult(
        "weekly_drawdown",
        ok,
        observed=round(c.weekly_drawdown_pct, 5),
        threshold=lim,
        detail="locked out" if c.weekly_locked else "",
    )


def g_monthly_drawdown(c: GateContext) -> GateResult:
    lim = c.settings.risk.max_monthly_drawdown_pct
    ok = not c.monthly_locked and c.monthly_drawdown_pct < lim
    return GateResult(
        "monthly_drawdown",
        ok,
        observed=round(c.monthly_drawdown_pct, 5),
        threshold=lim,
        detail="locked out" if c.monthly_locked else "",
    )


def g_exposure(c: GateContext) -> GateResult:
    r = c.settings.risk
    ok = (
        c.open_positions < r.max_concurrent_positions
        and c.open_risk_pct < r.max_total_open_risk_pct
    )
    return GateResult(
        "exposure",
        ok,
        observed=f"{c.open_positions} positions / {c.open_risk_pct:.3%} risk",
        threshold=f"< {r.max_concurrent_positions} positions and < {r.max_total_open_risk_pct:.1%}",
    )


def g_trade_frequency(c: GateContext) -> GateResult:
    lim = c.settings.risk.max_trades_per_day
    return GateResult(
        "trade_frequency",
        c.trades_today < lim,
        observed=c.trades_today,
        threshold=f"< {lim} per day",
    )


def g_no_duplicate(c: GateContext) -> GateResult:
    return GateResult(
        "no_duplicate",
        not c.duplicate_tag,
        observed="duplicate" if c.duplicate_tag else "unique",
        threshold="no existing position or order for this setup",
    )


# --------------------------------------------------------------------------------------
# Ordering: cheapest and most-likely-to-fail first
# --------------------------------------------------------------------------------------

MANDATORY_GATES: list[GateFn] = [
    g_kill_switch,
    g_broker_connection,
    g_symbol_resolved,
    g_data_freshness,
    g_daily_drawdown,
    g_weekly_drawdown,
    g_monthly_drawdown,
    g_exposure,
    g_trade_frequency,
    g_spread,
    g_session,
    g_news_blackout,
    g_news_risk,
    g_market_regime,
    g_has_candidate,
    g_strategy_validated,
    g_no_duplicate,
    g_htf_conflict,
    g_min_rr,
    g_stop_validity,
    g_premium_discount,
]


def run_gates(c: GateContext, gates: list[GateFn] | None = None) -> list[GateResult]:
    """Run every gate and return the full trace.

    All gates run even after one fails: "what else would have blocked this?" is a
    question the rejection ledger must be able to answer.
    """
    results: list[GateResult] = []
    for gate in gates or MANDATORY_GATES:
        try:
            results.append(gate(c))
        except Exception as exc:
            results.append(
                GateResult(
                    getattr(gate, "__name__", "unknown").removeprefix("g_"),
                    False,
                    observed=f"gate raised {type(exc).__name__}",
                    threshold="gate evaluates cleanly",
                    detail=str(exc),
                )
            )
    return results


def all_passed(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)


def first_failure(results: list[GateResult]) -> GateResult | None:
    return next((r for r in results if not r.passed), None)
