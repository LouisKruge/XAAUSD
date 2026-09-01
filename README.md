# XAUUSD Trading Intelligence & Execution System

A highly selective, multi-layered XAUUSD (Gold) trading system that integrates directly
with MetaTrader 5. It is **not** an indicator EA. It is an analysis pipeline that
produces an auditable decision — `NO_TRADE`, `A`, or `A+` — for every evaluation, and
routes only `A` / `A+` decisions to an execution layer guarded by hard risk invariants.

> **NO TRADE IS BETTER THAN A LOW-QUALITY TRADE.**
>
> The expected steady state of this system is *mostly idle*. In an 11-month synthetic
> backtest it produced 5 trades from 24,064 evaluations. If it trades often, something
> is wrong — check the rejection ledger first.

---

## Status

| | |
|---|---|
| **Phases complete** | 1–12 (foundation → dashboard) |
| **Tests** | 342 unit + 74 integration, including a backtest/live parity gate |
| **`LIVE_TRADING`** | `false`, and requires two-key arming to change |
| **Next** | Phase 13 (paper → demo, ≥4 weeks) and Phase 14 (small live) — both are wall-clock work that cannot be compressed |

Nothing here has traded real money, and nothing should until a strategy clears the
deployment gate on out-of-sample data (`docs/architecture/05-roadmap.md`, Phase 10).

---

## Quick start

**On Windows, without a terminal:** double-click `windows\Setup.bat` once, then use the
three Desktop shortcuts it creates. The dashboard's **System** tab runs the pre-flight
check, backtests and the validation gate as buttons. See `docs/DEPLOYMENT.md`. (The
Windows path is confirmed working through Phase 1 — setup, shortcuts, dashboard and the
System tab jobs; `stop.vbs` and live arming remain unexercised.)

Otherwise:

```bash
uv venv --python 3.11 && uv pip install -e ".[dev,ml,api]"

python -m xauusd.cli doctor                 # config, database and broker pre-flight
python -m xauusd.cli backtest --synthetic 30000 --step 6
python scripts/seed_demo_data.py 45         # populate a throwaway DB for the dashboard
python -m xauusd.cli dashboard              # http://127.0.0.1:8000
```

The dashboard binds to loopback. To reach it from another machine, tunnel it
(`ssh -N -L 8000:127.0.0.1:8000 you@host`) — it can halt the engine and flatten every
position, so binding it to a routable address without `XAUUSD_DASHBOARD__AUTH_TOKEN` is
refused at startup rather than served. See `docs/DEPLOYMENT.md`.

Answering the two questions the brief requires:

```bash
python -m xauusd.cli explain 1234           # why DID it take (or refuse) this trade?
python -m xauusd.cli rejections --hours 24  # why has it not traded?
```

---

## How it works

```
MarketView (point-in-time; cannot see the future)
      │
      ├─ structure · liquidity · FVG · order blocks · S/R · premium/discount
      ├─ sessions (DST-correct) · volatility · regime
      └─ macro (vintage-filtered) · DXY · yields · calendar · news
      ▼
strategies → FeatureVector → score /100 → calibrated probability
      ▼
21 hard gates (all must pass)  →  NO_TRADE / A / A+
      ▼
RiskGate — the single path to the broker
      ▼
idempotent MT5 execution → position management → decision journal
```

Every stage is described in `docs/architecture/`. The detection rules are specified in
`docs/specs/` before they were coded, because that is where trading judgement lives.

---

## The parts that matter most

**`MarketView`** (`src/xauusd/data/marketview.py`) is the class everything depends on
being right. A bar is visible only once it has *closed* at or before the evaluation
instant, so the forming bar — whose high and low are not yet known — can never be read.
Reaching forward raises `LookAheadError`. Property-tested across hundreds of instants.

**`RiskGate`** (`src/xauusd/risk/gate.py`) is the only path from a plan to an order.
Sizing floors lots, refuses to shrink a structural stop to fit `volume_min`, and
cross-checks our loss-per-lot against the broker's own `order_calc_profit` — refusing
to trade on a specification it cannot verify. An invariant violation raises and trips
the kill switch rather than clamping silently.

**`OrderManager`** (`src/xauusd/execution/order_manager.py`) never resends an ambiguous
send. Ground truth comes from the broker, by deterministic client tag; if it cannot be
established, trading halts and a human is alerted.

**The `decisions` table** is written on *every* evaluation, including the thousands
ending in `NO_TRADE`, with the full feature vector and ordered gate trace. It is
simultaneously the explainability record, the rejection ledger, and the training set
for the probability model.

---

## Non-negotiables, enforced in code

1. Risk capped at **1% (A)** / **2% (A+)** of live equity, from the broker's real spec.
2. No martingale, grid, averaging in, or widening stops — `modify_stop` raises on any
   attempt to widen, and the `Broker` interface has no method to average in at all.
3. Minimum **1:2** reward-to-risk, re-checked after repricing and tick rounding.
4. Daily 2% / weekly 5% / monthly 10% drawdown lockouts, measured from period **peak**.
5. A strategy cannot reach live routing without `OOS_PASSED` status in the database.
6. Live trading needs the config flag **and** an arming file matching the account.

Each has a test asserting the prohibited behaviour is impossible, not merely discouraged.

---

## Documentation

| | |
|---|---|
| `docs/architecture/` | System design, tech stack, MT5 integration, schema, data sources, roadmap, open decisions |
| `docs/specs/` | Objective detection rules — structure, liquidity/zones, scoring |
| `docs/DEPLOYMENT.md` | Stage-by-stage runbook, incident response, scaling schedule |
| `docs/FINDINGS.md` | Bugs found while building, and what each would have cost |

---

## An honest note on the 70% requirement

The brief requires ≥70% win rate in validation. Implemented literally as a 95% Wilson
*lower bound* of 70%, that gate is close to unreachable — even 700 wins in 1000 trades
(exactly 70% observed) has a lower bound of 67.1% and fails. So the gate requires the
observed rate ≥70% **and** the lower bound ≥60% **and** ≥100 out-of-sample trades, and
prints all three so the judgement is never hidden in a single pass/fail.

A ≥70% win rate at ≥1.8 average RR is roughly +1R expectancy per trade. Most strategy
versions will fail this gate. That is the gate working. If a variant passes on the first
attempt, hunt for a data leak before celebrating — the Phase 10 gate makes that a
required step.
