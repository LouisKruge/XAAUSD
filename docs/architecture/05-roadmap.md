# 05 — Implementation Roadmap

Fourteen phases. Each has a concrete deliverable and an **acceptance gate**; a phase is not
"done" because code exists, but because its gate passes. Phases 1–4 are foundation work with no
trading logic at all, which is deliberate: the value of this system is the trustworthiness of its
record, and that is decided by the plumbing.

Estimates assume focused work and are for sequencing, not commitments.

---

### Phase 1 — Skeleton, config, database, logging  *(~3–4 days)*

Repo layout, `pyproject.toml`/uv lock, pydantic-settings config with layered YAML, Alembic
migrations for the full schema in `03`, structlog JSON logging, Postgres+Timescale+Redis via
docker-compose, CI (ruff, mypy, pytest, secret scan), health endpoints, alert `Notifier`
(Telegram + email), config hashing and `config_versions` persistence.

**Gate:** `alembic upgrade head` builds the whole schema; a config with an invalid risk value
fails at startup with a clear message; a test alert is delivered.

---

### Phase 2 — MT5 bridge + broker abstraction  *(~5–7 days)*

gRPC contract, `mt5-bridge` (single-threaded, supervised, tick streaming), `Broker` protocol,
`Mt5GrpcBroker`, symbol auto-discovery and spec snapshotting, connection supervisor,
`SimBroker` and `PaperBroker` skeletons, the fake-bridge contract test suite, recorded-session
replay harness, broker-clock offset measurement.

**Gate:** connects to a demo account; auto-resolves the gold symbol from at least two different
brokers' symbol lists in test fixtures; spec snapshot persisted; contract tests cover every
mapped return code; engine-side unit tests run green on Linux with no terminal.

---

### Phase 3 — Data ingestion & the MarketView  *(~4–6 days)*

Bar poller with gap detection and back-fill, tick recorder, spread statistics, historical
harvest from the broker + Dukascopy into the Parquet lake, the `MarketView` cursor with hard
look-ahead prevention, Timescale continuous aggregates, DuckDB research helpers.

**Gate:** a property test proves `MarketView` cannot return a bar with `ts >= view.now` under any
access pattern; 8 years of M1 loaded and gap-audited; the broker-vs-Dukascopy basis report is
produced.

---

### Phase 4 — Sessions, clocks, volatility, regimes  *(~3–4 days)*

DST-correct session engine (Asia/London/NY/overlap, killzones), broker-offset handling, ATR and
realised-volatility measures, volatility bucketing, regime classifier (trend/range/vol state,
plus `ABNORMAL`), holiday and thin-liquidity detection.

**Gate:** session labels verified across both 2025 DST transition weeks and the two weeks where
UK/US DST disagree; regime classifier output reviewed against labelled historical periods.

---

### Phase 5 — Core price-action engines  *(~8–12 days — the analytical heart)*

Written to explicit specifications produced as part of this phase (`docs/specs/*.md`), each with
worked examples and unit tests on hand-labelled chart segments:

- **Market structure**: fractal/ATR-hybrid swing detection with a documented lookback and minimum
  swing displacement; HH/HL/LH/LL; BOS (body close beyond a *confirmed* swing with minimum
  displacement in ATR terms); CHOCH; MSS; internal vs external structure; strong vs weak highs/lows.
- **Liquidity**: BSL/SSL, EQH/EQL with an ATR-relative tolerance, PDH/PDL, PWH/PWL, session
  extremes, range boundaries; sweep detection with penetration, rejection and
  displacement-after criteria; stop hunts; false breakouts.
- **FVG**: bullish/bearish/inverse; unmitigated/partial/mitigated/invalidated lifecycle; quality
  scoring.
- **Order blocks**: bullish/bearish/breaker/mitigation; must be tied to a BOS/MSS to qualify;
  freshness, test count, invalidation.
- **S/R & supply/demand**: clustering with importance from timeframe, touches, rejection strength,
  recency.
- **Premium/discount**: dealing-range determination and equilibrium.

**Gate:** on a held-out set of manually annotated XAUUSD segments, structure and sweep detection
agree with the annotation ≥ 85% with no look-ahead; every engine is deterministic (same input →
same output) and re-runs identically after restart.

---

### Phase 6 — Fundamental, macro & news layer  *(~5–7 days)*

FRED ingestion with vintages, synthetic DXY, yields/real-yields state, macro regime classifier
(`STRONGLY_BULLISH` … `STRONGLY_BEARISH` for gold), calendar ingestion (MQL5 EA relay + provider +
YAML fallback) with our own impact mapping, blackout state machine with the
volatility/spread-normalisation exit condition, news pipeline with rules + LLM assessment and
frozen historical assessments, `news_risk_state` aggregation.

**Gate:** point-in-time reconstruction test — for a random historical timestamp, the macro/news
state contains nothing published after it; blackout windows correctly computed for 2 years of
historical FOMC/NFP/CPI releases.

---

### Phase 7 — Backtesting engine  *(~6–8 days)*

Event-driven backtester over `MarketView` + `SimBroker`, M1 intrabar resolution with a
conservative fallback, spread from recorded per-bar data, commission, a slippage model,
execution latency, full metric block, per-session/regime/year breakdowns, HTML report generation,
and the **live↔backtest parity replay test wired into CI**.

**Gate:** replaying a recorded paper session reproduces its decisions and scores exactly; a
deliberately look-ahead-biased test strategy is *caught* by the harness; cost sensitivity
(0.5×/1×/2× spread) reported for every run.

---

### Phase 8 — Setup detection, scoring, classification  *(~6–8 days)*

The `Strategy` plugin interface and the first strategy (`sweep_mss_fvg`), structural SL placement,
TP selection anchored to real opposing liquidity, RR computation and the 1:2 floor with 1:3
preference logic, the 100-point scoring engine with penalty rules, hard-gate framework with the
`gate_trace`, `A`/`A+` classification including the independent-category-breadth requirement, and
full `decisions` journalling.

**Gate:** every `NO_TRADE` in a 2-year backtest has an attributable blocking gate; no trade in any
run has RR < 2.0 after price normalisation; scoring is reproducible from the stored feature vector.

---

### Phase 9 — Risk engine & kill switch  *(~4–5 days)*

Position sizing with the MT5 cross-check, risk-state machine for day/week/month with correct
period anchoring in broker time, exposure limits, correlation/duplicate guards, drawdown lockouts,
the kill-switch state machine with manual-clear requirement, and the invariant assertions.

**Gate:** property tests across randomised specs/prices/SLs/equities prove risk never exceeds the
cap and lots are always broker-valid or the trade is rejected; simulated DD breaches produce
lockout, alert and dashboard state; every "NEVER ALLOWED" behaviour has a test asserting it is
impossible (including: martingale sizing, stop widening, averaging in, duplicate entry).

---

### Phase 10 — Validation & the deployment gate  *(~7–10 days, then ongoing)*

In-sample / out-of-sample split with a genuinely untouched OOS period, anchored and rolling
walk-forward, Monte Carlo (trade-order shuffle, bootstrap, random-start), parameter sensitivity
surfaces, spread/slippage/latency stress, and the probability model: triple-barrier labelling
(+2R / −1R / time), purged & embargoed CV, LightGBM with monotonic constraints, isotonic
calibration, reliability curves, and MLflow tracking.

**The deployment gate** — a strategy version reaches `OOS_PASSED` only if, on out-of-sample data
it was never fitted on:

```
1.  ≥ 100 OOS trades                       (below this, nothing is measurable)
2a. OBSERVED win rate ≥ 0.70                      ← the brief's stated bar
2b. Wilson 95% LOWER BOUND of win rate ≥ 0.60     ← evidence it is not a fluke
3.  Profit factor ≥ 2.0
4.  Expectancy ≥ +0.40R per trade
5.  Max drawdown ≤ 15% at the configured risk
6.  Realised average RR ≥ 1.8
7.  Sharpe ≥ 1.5, Sortino ≥ 2.0 (trade-level, annualised)
8.  Max consecutive losses ≤ 8, and survivable at 2% risk
9.  Risk of ruin < 1% at the configured risk
10. Walk-forward efficiency ≥ 0.5 (OOS performance vs IS)
11. Profitable in ≥ 70% of walk-forward windows
12. Monte Carlo 5th percentile of final equity still positive
13. Parameter sensitivity: performance degrades smoothly, no isolated spikes
14. Still passes 1–5 at 2× modelled spread and 2× slippage
15. Probability model calibration: Brier ≤ baseline, calibration slope in [0.8, 1.2]
```

**Read this honestly.** A ≥70% win-rate *lower bound* combined with ≥1.8 average RR implies an
expectancy near +1R per trade. That is an extraordinary standard, and the realistic outcome is
that most strategy versions **fail this gate**. That is the gate working. Three consequences you
should expect and accept up front:

- The system may spend a long time in Phases 8–10 with nothing approved for live.
- Reaching the gate usually requires the strategy to be *rarer*, not better — often only a handful
  of trades per month.
- If a variant clears all fifteen criteria on the first attempt, the correct response is suspicion
  of a leak, not celebration. A leak hunt is a mandatory step before any `OOS_PASSED` verdict.

Passing the gate makes a strategy *eligible*, not *expected to repeat*. Live performance is
monitored against the OOS distribution and degrades the strategy automatically if it drifts.

**Gate:** at least one strategy version has a complete, reproducible `validation_reports` row with
every criterion evaluated — pass or fail. A `FAILED` verdict is a legitimate and expected output
of this phase.

---

### Phase 11 — Execution & position management  *(~5–7 days)*

Order manager with deterministic client tags and full idempotency, pre-send checklist,
return-code taxonomy, ambiguous-send reconciliation, position manager (server-side stops, BE,
partials, structural trailing, time stop, invalidation exit), the 60-second reconciler, and
startup reconcile-before-trade.

**Gate:** a chaos suite — kill the bridge mid-send, drop the network during `order_send`, return
a requote, return a partial fill, desync the DB — and in every case the system ends in a correct,
reconciled state and never opens a duplicate position.

---

### Phase 12 — Dashboard  *(~7–10 days)*

FastAPI + WebSocket backend; React/TS/Tailwind frontend on the institutional dark palette:
Command Centre, Live Market Intelligence (with lightweight-charts overlays for FVGs, OBs,
liquidity, structure), Trade Candidate panel with score breakdown and reasons for/against,
**Rejection Ledger**, Performance Analytics (equity, drawdown, A vs A+, session/regime/setup
breakdowns), Risk & Kill Switch panel, System Health, and the Decision Explorer for the
"why / why not" queries.

**Gate:** the dashboard cannot write to the DB except through the two audited safety commands;
restarting or crashing it demonstrably does not affect a running engine; every panel renders from
real Phase 10 backtest data.

---

### Phase 13 — Paper → Demo  *(≥ 4 weeks wall clock, and it cannot be compressed)*

Stage 4 paper trading on live data with simulated fills, then Stage 5 on a real MT5 demo account.
Daily comparison of realised vs OOS-expected metrics; slippage distribution fitted from real demo
fills and fed back into the backtest cost model; parity checks; operational drills (VPS reboot,
broker maintenance window, weekend gap).

**Gate:** ≥ 30 demo trades; realised expectancy within the Monte Carlo confidence band of the OOS
distribution; zero execution defects; zero unexplained reconciliation differences; slippage
model updated and validation re-run with the *measured* costs.

---

### Phase 14 — Small live, then gradual scaling  *(ongoing)*

Stage 6 with the two-key arming, an initial hard risk cap well below 1% (e.g. 0.25%) regardless of
classification, and a small account. Scaling steps are pre-defined and mechanical — e.g. raise the
cap one step only after N trades within expectation and no operational incidents — never
discretionary after a good week. Continuous monitoring: model health, drift, regime shifts,
monthly re-validation, and automatic downgrade to observation-only if live diverges from OOS.

**Gate:** each scaling step has a written pre-condition, and the system enforces the current cap
in code rather than trusting configuration discipline.

---

## Sequencing notes

- **Phases 1–4 are non-negotiable prerequisites.** Building strategy logic on an unreliable
  `MarketView` produces validation numbers that mean nothing, and you will not find out for months.
- **Phase 5 is where the calendar time really goes.** Objective, non-repainting SMC detection is
  genuinely hard, and the specifications in `docs/specs/` are as much of the deliverable as the code.
- **One strategy end-to-end before a second.** A single strategy taken all the way to Phase 10
  teaches more than four half-built ones, and the second is then fast.
- **The dashboard could move earlier** if you want visibility during Phase 5–8 development; a
  minimal read-only version after Phase 7 is a reasonable trade. Say the word and I will resequence.
