"""Populate the database with realistic decisions, trades and equity history.

For dashboard development and for verifying the explainability screens without waiting
for a paper-trading run to accumulate data. Writes to the configured database; use a
throwaway one.
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauusd.config.settings import load_settings
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import (
    Classification,
    Direction,
    ExitReason,
    Timeframe,
)
from xauusd.domain.types import (
    Decision,
    GateResult,
    ScoreBreakdown,
    SizingResult,
    TargetLevel,
    TradePlan,
)

UTC = UTC
CATEGORIES = {
    "htf_bias": 15.0,
    "market_structure": 15.0,
    "liquidity": 15.0,
    "fvg_ob": 10.0,
    "support_resistance": 10.0,
    "fundamentals": 10.0,
    "dxy_yields": 5.0,
    "session": 5.0,
    "volatility_regime": 5.0,
    "entry_confirmation": 10.0,
}
BLOCKERS = [
    ("has_candidate", 0.52),
    ("session", 0.19),
    ("news_blackout", 0.08),
    ("min_rr", 0.06),
    ("score_a", 0.05),
    ("market_regime", 0.04),
    ("spread", 0.03),
    ("premium_discount", 0.02),
    ("htf_conflict", 0.01),
]


def breakdown(rng: random.Random, quality: float) -> ScoreBreakdown:
    cats, strong = {}, []
    for name, mx in CATEGORIES.items():
        v = round(min(mx, max(0.0, rng.gauss(mx * quality, mx * 0.18))), 2)
        cats[name] = v
        if v >= 0.7 * mx:
            strong.append(name)
    penalties = {}
    if rng.random() < 0.35:
        penalties["news_risk"] = round(rng.uniform(1, 4), 2)
    if rng.random() < 0.2:
        penalties["wide_spread"] = round(rng.uniform(1, 4), 2)
    total = max(0.0, sum(cats.values()) - sum(penalties.values()))
    return ScoreBreakdown(cats, dict(CATEGORIES), penalties, round(total, 2), tuple(strong))


def main(days: int = 45, seed: int = 4) -> None:
    rng = random.Random(seed)
    settings = load_settings()
    db = Database(settings.database.url)
    db.create_all()

    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    equity = 10_000.0
    price = 2650.0
    decision_id_for_trade: list[int] = []

    with db.session() as s:
        repos = Repositories(s)
        for name in ("sweep_mss_fvg", "sweep_mss_ob"):
            repos.strategy_status.set_status(
                name,
                "1.0",
                "DEMO",
                max_class="A",
                approved_regimes=["STRONG_BULL", "RANGE"],
                approved_sessions=["LONDON", "NEW_YORK", "OVERLAP"],
            )
        repos.strategy_status.set_status("pdh_pdl_reversion", "1.0", "DEV")
        repos.strategy_status.set_status("session_range_expansion", "1.0", "FAILED")

        t = start
        n_eval = 0
        while t < now:
            t += timedelta(minutes=5)
            if t.weekday() >= 5:
                continue
            hour = t.hour
            if not (7 <= hour <= 21):
                continue
            n_eval += 1
            price += rng.gauss(0.0, 1.1)

            # Roughly one A/A+ decision per two days — this system is meant to be idle.
            is_trade = rng.random() < 0.0016
            if not is_trade:
                r = rng.random()
                acc = 0.0
                gate = "has_candidate"
                for name, share in BLOCKERS:
                    acc += share
                    if r <= acc:
                        gate = name
                        break
                repos.decisions.save(
                    Decision(
                        ts=t,
                        symbol="XAUUSD",
                        classification=Classification.NO_TRADE,
                        mode="DEMO",
                        gates=(GateResult(gate, False, "blocked", "pass"),),
                        reasons_against=(f"{gate} blocked this evaluation",),
                        features={"regime": "STRONG_BULL", "session": "LONDON"},
                        config_hash=settings.config_hash(),
                    )
                )
                continue

            direction = Direction.LONG if rng.random() < 0.58 else Direction.SHORT
            quality = rng.uniform(0.70, 0.95)
            bd = breakdown(rng, quality)
            cls = (
                Classification.A_PLUS
                if bd.total >= 85 and len(bd.strong_categories) >= 7
                else Classification.A
            )
            risk_dist = rng.uniform(4.0, 9.0)
            entry = price
            sl = entry - risk_dist * direction.sign
            rr = rng.uniform(2.0, 3.4)
            tp = entry + risk_dist * rr * direction.sign
            plan = TradePlan(
                "sweep_mss_fvg" if rng.random() < 0.6 else "sweep_mss_ob",
                "1.0",
                direction,
                entry,
                sl,
                (
                    TargetLevel(
                        tp, rr, "PDH liquidity" if direction is Direction.LONG else "PDL liquidity"
                    ),
                ),
                t,
                Timeframe.M15,
                f"a M15 close beyond {sl:.2f} invalidates the structure shift",
                symbol="XAUUSD",
            )
            risk_pct = 0.01 if cls is Classification.A else 0.02
            risk_money = equity * risk_pct
            lots = round(risk_money / (risk_dist * 100), 2)
            did = repos.decisions.save(
                Decision(
                    ts=t,
                    symbol="XAUUSD",
                    classification=cls,
                    mode="DEMO",
                    plan=plan,
                    score=bd.total,
                    breakdown=bd,
                    probability=round(rng.uniform(0.55, 0.78), 4),
                    model_id="prob-20260601-000000",
                    model_health="HEALTHY",
                    features={
                        "sweep_quality": round(rng.uniform(0.6, 1.0), 3),
                        "htf_alignment": 1.0,
                        "has_mss": 1,
                        "premium_discount_position": round(rng.uniform(0.1, 0.4), 3),
                    },
                    gates=tuple(
                        GateResult(g, True, "ok", "pass")
                        for g in (
                            "kill_switch",
                            "broker_connection",
                            "data_freshness",
                            "spread",
                            "session",
                            "news_blackout",
                            "market_regime",
                            "min_rr",
                            "stop_validity",
                            "premium_discount",
                        )
                    ),
                    reasons_for=(
                        f"liquidity sweep quality {rng.uniform(0.7, 1.0):.2f}",
                        "market structure shift confirmed with displacement",
                        f"entry in {'discount' if direction is Direction.LONG else 'premium'}",
                        f"reward-to-risk {rr:.2f} to opposing liquidity",
                    ),
                    reasons_against=("macro backdrop neutral",),
                    sizing=SizingResult(
                        True,
                        lots,
                        risk_money,
                        risk_pct,
                        risk_dist,
                        risk_dist * 100,
                        7 * lots * 2,
                        1.5,
                        risk_money + 7 * lots * 2,
                        "ok",
                    ),
                    config_hash=settings.config_hash(),
                )
            )

            # Resolve it. ~62% winners at these RRs.
            won = rng.random() < 0.62
            r_mult = rr if won else -1.0
            pnl = risk_money * r_mult
            equity += pnl
            opened = t
            closed = t + timedelta(hours=rng.uniform(1.5, 14))
            pos = repos.positions.open_position(
                mt5_position=1000 + len(decision_id_for_trade),
                decision_id=did,
                strategy=plan.strategy,
                classification=str(cls),
                symbol="XAUUSD",
                side=str(direction),
                opened_at=opened,
                entry_price=entry,
                initial_sl=sl,
                initial_tp=tp,
                current_sl=sl,
                current_tp=tp,
                volume=lots,
                remaining_volume=lots,
                risk_money=risk_money,
                risk_pct=risk_pct,
                session="LONDON" if hour < 13 else "NEW_YORK",
                regime="STRONG_BULL",
            )
            repos.positions.close(
                pos,
                exit_price=tp if won else sl,
                exit_reason=str(ExitReason.TAKE_PROFIT if won else ExitReason.STOP_LOSS),
                gross_pnl=pnl,
                commission=7 * lots * 2,
                closed_at=closed,
            )
            pos.mae_r = round(rng.uniform(0.1, 0.8), 3)
            pos.mfe_r = round(max(r_mult, 0) + rng.uniform(0, 0.4), 3)
            pos.bars_held = int(rng.uniform(20, 160))
            decision_id_for_trade.append(did)
            price = tp if won else sl

            repos.risk.snapshot_account(
                ts=closed,
                balance=equity,
                equity=equity,
                margin=0.0,
                free_margin=equity,
                margin_level=0.0,
                open_positions=0,
                open_risk_pct=0.0,
                currency="USD",
                mode="DEMO",
            )

    print(
        f"seeded {n_eval} evaluations, {len(decision_id_for_trade)} trades, "
        f"final equity ${equity:,.2f}"
    )
    print(f"database: {settings.database.url}")


if __name__ == "__main__":
    main(days=int(sys.argv[1]) if len(sys.argv) > 1 else 45)
