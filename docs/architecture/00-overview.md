# 00 — System Architecture

## 1. Design theses

Five decisions shape everything else. They are stated up front because most of the rest of this
document is a consequence of them.

**T1 — The decision pipeline is a pure function of a point-in-time view of the world.**
Every analyser consumes a `MarketView` object that is *physically incapable* of returning data
timestamped after the evaluation instant. Look-ahead bias is prevented structurally, not by
discipline. The same analyser code runs unchanged in backtest, paper, demo and live; only the
`MarketView` and `Broker` implementations differ. This is the single most important property of
the system, because it is what makes the validation numbers mean anything.

**T2 — Analysis is bar-close driven; execution is tick-aware.**
The decision engine wakes on the close of an M5 bar (and M1 when a setup is armed). It never
evaluates mid-bar, because a mid-bar high/low is not reproducible in a backtest and invites
repainting. Ticks are consumed separately by a lightweight monitor for spread, price freshness,
stop management and kill-switch triggers.

**T3 — The MT5 Python binding is a hostile dependency and is quarantined.**
The `MetaTrader5` package is Windows-only, process-global, not thread-safe, and requires a running
terminal. It is confined to a single dedicated process behind a narrow RPC interface. The
intelligence engine never imports it. This is what lets the engine be developed and tested on
Linux/macOS, unit-tested without a terminal, and deployed against a Windows VPS.

**T4 — Risk is a choke point, not a policy.**
There is exactly one code path from a trade plan to a broker order, and it runs through
`RiskGate`. The gate re-derives equity, exposure, drawdown state and sizing from primary sources
at call time — it never trusts values computed earlier in the pipeline. Violating an invariant
raises and trips the kill switch rather than clamping silently.

**T5 — Scores are ordinal; probabilities are calibrated.**
A confluence score out of 100 is a *ranking* device, not a probability. The mapping from score
(plus features) to "probability this setup reaches +2R before -1R" is fitted on out-of-sample
outcomes with purged, embargoed cross-validation, and its calibration is measured. Nothing in the
system hardcodes a win probability.

---

## 2. End-to-end flow

```
                          ┌──────────────────────────────────────────────┐
                          │            EXTERNAL DATA SOURCES             │
                          │  MT5 feed · FRED · calendar · news/RSS · FX  │
                          └───────────────────────┬──────────────────────┘
                                                  │
                    ┌─────────────────────────────▼─────────────────────────────┐
                    │                  INGESTION LAYER                          │
                    │  bar poller · tick monitor · macro worker · news worker    │
                    │  → normalise → timestamp (UTC + vintage) → persist         │
                    └─────────────────────────────┬─────────────────────────────┘
                                                  │
                    ┌─────────────────────────────▼─────────────────────────────┐
                    │      MarketView (point-in-time, no future data)           │
                    └─────────────────────────────┬─────────────────────────────┘
                                                  │
   ┌──────────────────────────────────────────────┼──────────────────────────────────────────┐
   │                              ANALYSIS LAYER  │  (all pure, all cached, all versioned)   │
   │                                              │                                          │
   │  PRICE-DERIVED                    CONTEXT-DERIVED                  EXOGENOUS            │
   │  ├── swing/structure engine       ├── session engine               ├── DXY / USD state  │
   │  │   (HH/HL/LH/LL, BOS,           │   (Asia/London/NY, overlap,    ├── yields & real    │
   │  │    CHOCH, MSS, strong/weak,    │    killzones, DST-correct)     │   yields (2y/10y)  │
   │  │    internal vs external)       ├── volatility & regime engine   ├── macro regime     │
   │  ├── liquidity engine             │   (trend/range, vol buckets,   │   classifier       │
   │  │   (BSL/SSL, EQH/EQL, PDH/PDL,  │    abnormal/unstable)          ├── econ calendar    │
   │  │    PWH/PWL, sweeps, stop       ├── spread/liquidity-of-market   │   (blackout state) │
   │  │    hunts, false breaks)        │   quality monitor              └── news & geopolitics│
   │  ├── FVG engine                   └── ATR / displacement metrics       (risk level only)│
   │  ├── order block / breaker engine                                                       │
   │  ├── S/R + supply/demand engine                                                         │
   │  └── premium/discount (dealing range) engine                                            │
   │                                              │                                          │
   └──────────────────────────────────────────────┼──────────────────────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       MTF CONFLUENCE ASSEMBLER                       │
                       │  M/W/D bias → H4/H1 structure → M15 setup →          │
                       │  M5 confirmation, with explicit agreement/conflict    │
                       │  flags per level. Produces a FeatureVector.           │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       SETUP DETECTORS (strategy plugins)             │
                       │  Each emits 0..n TradePlan candidates with entry,    │
                       │  structural SL, structural TP ladder, invalidation.  │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       SCORING ENGINE  → score /100 + breakdown       │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       PROBABILITY MODEL → calibrated p(+2R first)    │
                       │       + calibration confidence / model health        │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       HARD FILTERS (veto gates — all must pass)      │
                       │  connection · data freshness · symbol · trade-allowed│
                       │  kill switch · spread · news blackout · regime       │
                       │  whitelist · session whitelist · RR ≥ 2 · SL validity│
                       │  · drawdown limits · exposure · correlation · dupes  │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       CLASSIFIER →  NO_TRADE | A | A+                │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       RISK GATE + POSITION SIZING                    │
                       │  live equity → risk % → broker specs → lots          │
                       │  → invariant assertions → approve or reject          │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │       MT5 EXECUTION (idempotent, reconciled)         │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
                       ┌──────────────────────────▼──────────────────────────┐
                       │  POSITION MANAGER (BE, partials, trail, time stop,   │
                       │  invalidation exit) · TRADE MONITOR · RECONCILER     │
                       └──────────────────────────┬──────────────────────────┘
                                                  │
        ┌─────────────────────────────────────────▼─────────────────────────────────────────┐
        │  DECISION JOURNAL (every evaluation, taken or not) → POSTGRES → ANALYTICS →        │
        │  DASHBOARD · ALERTS · PERIODIC RE-VALIDATION & RECALIBRATION                        │
        └───────────────────────────────────────────────────────────────────────────────────┘
```

Critically, **every arrow above is also traversed in backtest**, using the same classes. The only
substitutions are `MarketView` (replayed instead of live) and `Broker` (simulated instead of MT5).

---

## 3. Process topology

Four OS processes, deliberately isolated so that a dashboard crash cannot affect trading.

```
┌───────────────────────── Windows VPS (or Windows host) ────────────────────────┐
│                                                                                │
│   ┌──────────────────┐        gRPC (localhost or WireGuard)                    │
│   │  mt5-bridge      │◄───────────────────────────────────────┐                │
│   │  · single thread │                                        │                │
│   │  · owns the only │   MetaTrader5 terminal (running)        │                │
│   │    MetaTrader5   │──────────────┐                          │                │
│   │    import        │              │                          │                │
│   │  · request queue │              ▼                          │                │
│   │  · calendar EA   │        broker servers                   │                │
│   │    listener      │                                         │                │
│   └──────────────────┘                                         │                │
└────────────────────────────────────────────────────────────────┼────────────────┘
                                                                 │
┌────────────────── engine host (same VPS, or Linux box) ─────────┼────────────────┐
│                                                                 │                │
│   ┌───────────────────────────┐      ┌──────────────────────┐   │                │
│   │  engine  (asyncio)        │─────►│  postgres +          │   │                │
│   │  · scheduler / clock      │      │  timescaledb         │   │                │
│   │  · ingestion              │◄─────│                      │   │                │
│   │  · analysis + decision    │      └──────────────────────┘   │                │
│   │  · risk gate              │      ┌──────────────────────┐   │                │
│   │  · execution client ──────┼─────►│  redis               │   │                │
│   │  · position manager       │      │  · pub/sub event bus │   │                │
│   │  · kill switch owner      │      │  · hot state cache   │   │                │
│   └───────────────────────────┘      │  · single-instance   │   │                │
│                ▲                     │    advisory lock     │   │                │
│                │                     └──────────┬───────────┘   │                │
│   ┌────────────┴──────────────┐                 │               │                │
│   │  worker  (APScheduler)    │                 │               │                │
│   │  · FRED / macro pulls     │                 │               │                │
│   │  · calendar refresh       │                 │               │                │
│   │  · news poll + LLM assess │                 │               │                │
│   │  · nightly analytics      │                 │               │                │
│   │  · weekly recalibration   │                 │               │                │
│   └───────────────────────────┘                 │               │                │
│                                                 ▼               │                │
│   ┌───────────────────────────┐      ┌──────────────────────┐   │                │
│   │  api (FastAPI + uvicorn)  │◄─────┤  read-only DB user   │   │                │
│   │  · REST + WebSocket       │      └──────────────────────┘   │                │
│   │  · serves React dashboard │                                 │                │
│   │  · CANNOT place orders    │      (except two explicit,       │                │
│   │    except kill/flatten    │       authenticated safety ops)  │                │
│   └───────────────────────────┘                                 │                │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Why the dashboard API is a separate process with a read-only DB role:** the dashboard is the
component most likely to be edited, restarted and experimented with. It must not be able to
deadlock, block or crash the trading loop. Its only write paths are two explicitly audited
commands — *trip kill switch* and *flatten all* — which are sent as messages on Redis and executed
by the engine, never by the API itself.

**Single-instance guarantee.** Two engines against one account would be catastrophic (duplicate
positions, double risk). The engine acquires a Redis lock *and* a Postgres advisory lock on
startup and refreshes them on a heartbeat. Failure to acquire = refuse to start. Loss of the lock
mid-run = immediate kill switch and flatten-or-hold per config.

---

## 4. Timing, clocks and the event loop

Gold trades ~23h/day, and the broker's server clock is usually not UTC and shifts with US DST.
Getting this wrong silently corrupts every session statistic, so it is handled explicitly.

**Four clocks, one source of truth.**

| Clock | Use |
|---|---|
| `UTC` | Canonical. Every timestamp in the database is `timestamptz` stored as UTC. |
| Broker server time | Only for interpreting MT5 bar timestamps and calendar-of-record. Offset is *measured*, not configured: `offset = broker_time_of_last_tick − utc_now`, sampled continuously and stored, with a discontinuity alarm at DST rollovers. |
| `Europe/London` | London session boundaries and DST. Derived with `zoneinfo`, never a fixed offset. |
| `America/New_York` | NY session, NY midnight (a structurally significant reference), CME open, macro release times. |

Sessions are computed from tz-aware local times, so the London open moves correctly relative to
UTC across the year and the two annual weeks where London and New York DST disagree are handled
automatically rather than being three days of corrupt statistics.

**The loop.**

```
every 1s   tick monitor    : refresh quote, spread, staleness, kill-switch triggers,
                             armed-position management (BE / partial / trail)
on M1 close: (only when a setup is ARMED) execution refinement + entry trigger check
on M5 close: full decision cycle   ← the main heartbeat
on M15/H1/H4/D1/W1/MN1 close: invalidate + recompute that timeframe's cached analysis
every 60s  : broker reconciliation (positions/orders vs internal state)
every 5m   : macro/news freshness check, calendar window recompute
nightly    : analytics rollup, journal integrity check, model health report
weekly     : walk-forward extension + probability recalibration (proposal only, never auto-deploy)
```

A decision cycle is budgeted at **< 2 seconds** wall clock. Higher-timeframe analysis is cached
and only recomputed on that timeframe's close, so the M5 cycle is dominated by M5/M15 work.

---

## 5. The decision pipeline in detail

### 5.1 Stage order matters

Cheap vetoes run first. If the kill switch is tripped, or spread is 90 points, there is no reason
to compute order blocks. But — and this is deliberate — **rejected candidates are still journalled
with the reason**, so `06` "why no trade?" queries work even for early exits. The pipeline records
a `gate_trace`: an ordered list of every gate evaluated, its verdict, and the values it saw.

```
Stage 0  PRE-FLIGHT     connection · data freshness · symbol resolved · terminal trade-allowed
                        · kill switch · single-instance lock · account currency known
Stage 1  ENVIRONMENT    spread · volatility regime · session · calendar blackout · news risk
Stage 2  CONTEXT        MN/W/D bias · H4/H1 structure · dealing range · premium/discount
Stage 3  NARRATIVE      liquidity map · sweep detection · displacement · MSS/BOS/CHOCH
Stage 4  SETUP          detector plugins → TradePlan candidates (entry / SL / TP ladder)
Stage 5  STRUCTURE-RR   structural SL validity · TP anchored to real liquidity · RR ≥ 2.0
Stage 6  SCORE          weighted confluence score + per-category breakdown + penalties
Stage 7  PROBABILITY    calibrated model → p(+2R before −1R) + model-health flag
Stage 8  CLASSIFY       NO_TRADE / A / A+ against thresholds (score AND probability AND gates)
Stage 9  RISK           equity · drawdown budgets · exposure · sizing · invariants
Stage 10 EXECUTE        idempotent submit · confirm · persist · arm position manager
```

A candidate must survive **all** stages. Any single failure at stages 0–9 produces `NO_TRADE`
with a fully attributed reason chain.

### 5.2 Classification requires agreement of independent authorities

`A` and `A+` are not just score thresholds. Classification requires the conjunction of:

| Requirement | `A` | `A+` |
|---|---|---|
| All hard gates pass | required | required |
| Confluence score | ≥ `A_score_min` | ≥ `Aplus_score_min` |
| Calibrated probability | ≥ `A_prob_min` | ≥ `Aplus_prob_min` |
| Reward-to-risk | ≥ 2.0 | ≥ 2.0 (3.0 preferred where structure supports it) |
| Independent confluence categories scoring "strong" | ≥ `A_categories` | ≥ `Aplus_categories` |
| HTF bias conflict | not permitted | not permitted |
| Fundamental (DXY/yields) alignment | neutral-or-better | must be aligned |
| News risk level | ≤ MODERATE | ≤ LOW |
| Strategy's own OOS validation status | PASSED | PASSED |
| Regime is on the strategy's validated whitelist | required | required |
| Max risk permitted | 1.0% equity | 2.0% equity |

The "independent categories" requirement exists because a single strong signal can inflate a
weighted score. Requiring *breadth* across categories that are not derived from each other is a
much better proxy for genuine confluence than a high total. Actual threshold values are config,
set from validation output in Phase 8, not guessed now.

**A+ never means "risk 2% automatically."** 2% is a ceiling. The sizing layer takes
`min(class_cap, remaining_daily_budget, remaining_weekly_budget, remaining_monthly_budget,
exposure_headroom, confidence_scaled_risk)`.

### 5.3 Strategy plugins

A strategy is a self-contained plugin implementing:

```python
class Strategy(Protocol):
    name: str
    version: str
    allowed_regimes: frozenset[Regime]
    allowed_sessions: frozenset[SessionWindow]

    def detect(self, view: MarketView, ctx: AnalysisContext) -> list[TradePlan]: ...
```

Strategies are registered, versioned, and each carries a `validation_status` row in the database.
`OOS_PASSED` is a precondition for live routing — enforced in code, so a half-tested idea
physically cannot reach the broker. Planned initial set (built and validated one at a time, not
all at once):

1. **`sweep_mss_fvg`** — HTF-aligned liquidity sweep → displacement → MSS → retrace into the
   displacement FVG / order block, in discount (long) or premium (short).
2. **`sweep_mss_ob`** — same narrative, order-block entry with breaker fallback.
3. **`session_range_expansion`** — Asian-range sweep at London open with continuation into the
   HTF draw on liquidity.
4. **`pdh_pdl_reversion`** — previous-day-level sweep and rejection in a validated range regime.

Each is judged on its own merits. It is fully expected that some fail validation and are never
deployed; that outcome is a success of the process, not a failure.

---

## 6. Backtest / live parity

The system exists to produce trustworthy statistics, so parity is enforced mechanically:

- **One code path.** `Strategy`, scoring, gates, risk and sizing are identical objects in both
  modes. A backtest is a different `Clock`, `MarketView` and `Broker` — nothing else.
- **The `MarketView` cursor cannot see the future.** It exposes `bars(tf, n)` returning only bars
  *closed* at or before `view.now`, and raises on any attempt to index forward. In backtest it is
  a cursor over a numpy/Parquet store; in live it is a cache over the DB and the bridge.
- **Macro and calendar data is vintage-aware.** Every macro observation stores both `ref_date` and
  `release_ts`. A backtest at time *t* only sees observations with `release_ts <= t`. Without
  this, revised CPI or a calendar row that already knows the *actual* leaks the future into the
  past — this is the most common and most invisible leak in fundamental backtests.
- **News assessments are frozen at first-seen.** An LLM assessment is stored with the timestamp it
  was produced and never regenerated for historical bars.
- **Intrabar resolution is explicit and conservative.** When SL and TP both fall inside one bar's
  range, the backtest resolves the *loss* first unless M1 data proves otherwise. Where M1 data
  exists, the engine replays M1 inside the decision bar for exact sequencing.
- **Costs are modelled, not assumed.** Spread from recorded per-bar spread (MT5 provides it),
  commission from the account's real schedule, slippage from a distribution fitted to actual
  demo/live fills once available, plus a configurable execution latency.
- **A parity test is part of CI.** A recorded live session is replayed through the backtest engine;
  decisions and scores must match bit-for-bit. Divergence fails the build. This is the guard that
  keeps the two paths from quietly drifting apart over months of development.

---

## 7. Failure model

| Failure | Detection | Response |
|---|---|---|
| MT5 terminal down / RPC unreachable | bridge health ping, 3 consecutive misses | Kill switch `BROKER_UNREACHABLE`; no new entries; existing positions rely on broker-side SL (which is always set server-side) |
| Stale quotes | last tick age > threshold, per-session aware | Kill switch `STALE_DATA` |
| Abnormal spread | spread > `n`×rolling median or > absolute cap | Block entries; suspend trailing; alert |
| Engine crash | supervisor (NSSM/systemd) + heartbeat row | Restart; on boot, **reconcile before anything else** |
| Engine/broker state divergence | 60s reconciler diff | Kill switch `STATE_DIVERGENCE`, alert, require manual clear |
| Order rejected (invalid stops / no money / market closed) | return code taxonomy | Classified retry vs abort; never a blind retry |
| Requote / partial fill | `TRADE_RETCODE_REQUOTE`, volume mismatch | Re-price within a bounded envelope, max N attempts, then abandon and journal |
| Ambiguous send (timeout, connection drop mid-send) | no confirmation received | **Never resend.** Query positions/orders by deterministic client tag first; only act on ground truth |
| DD limit breached | risk state machine | Lockout for the period; alert; requires the period to roll over |
| Model health degraded (live calibration drifts from OOS) | rolling Brier/PSI monitor | Downgrade to `A`-only, then to observation-only; alert |
| Clock/DST discontinuity | broker offset jump detector | Pause one cycle, re-derive sessions, alert |

Two disciplines run through all of it: **stop losses are always server-side on the broker** (so a
dead engine cannot mean an unprotected position), and **reconcile-before-act on every startup**.

---

## 8. Explainability

Every evaluation writes one `decisions` row, whether it trades or not. That row carries:

- the resolved `MarketSnapshot` id (structure, liquidity, FVG/OB, S/R, regime, session as of that instant)
- the complete `FeatureVector` as JSONB
- the ordered `gate_trace` with each gate's verdict and observed values
- the score breakdown per category, plus penalties applied
- the model probability, model id, and calibration health
- the classification and, for a rejection, the *first blocking gate* plus all others that would
  also have blocked
- `config_hash` and `git_sha`, so any decision can be replayed exactly

This makes both required questions pure database queries:

*"Why did the bot enter this trade?"* → render the score breakdown and gate trace for the decision
linked to the trade.

*"Why did the bot NOT take this trade?"* → query decisions in a time window; each names its
blocking conditions. The dashboard renders this as a rejection ledger, which is genuinely the most
useful screen in the system during Phases 4–6: it is how you find out that your bot is idle
because of a bug rather than because of discipline.

An optional LLM layer renders the stored structured record into prose on request. It reads the
journal; it never participates in the decision.

---

## 9. Security & safety posture

- Credentials only from environment / OS keyring — never in the repo, never in the database.
  `.gitignore` blocks `.env`, and CI runs a secret scan.
- `LIVE_TRADING=false` by default. Enabling live requires **two keys**: the config flag *and* a
  separate arming file containing the account number, which must match the connected account.
  A config edit alone cannot arm live trading.
- The bridge binds to localhost by default; remote use goes over WireGuard with mTLS, never a
  public port.
- The dashboard requires auth even on localhost, and its two write commands are audited.
- MT5's "AutoTrading" and the account's `trade_allowed`/`trade_expert` flags are checked every
  cycle — the terminal itself is a hardware kill switch.
- Structured logs (JSON) with rotation; no secrets in logs; every order attempt logged before and
  after send with the client tag.
