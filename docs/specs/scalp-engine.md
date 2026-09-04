# Short-Duration Scalp Engine — design spec

Status: **proposal, not implemented.** Written before code, per CLAUDE.md.

The existing A/A+ engine is not modified by anything here. No threshold is lowered, no
risk invariant is relaxed, no gate is deleted. The scalp engine is a second strategy
layer with its own structure model, its own score, its own risk tier and its own
validation record, sharing the analysis, risk and execution machinery that already
exists.

**Operating shape (§D):** no daily trade-count cap, up to 10 concurrent positions at
0.15% risk each, wins targeting 0.10–0.25% of capital. The 2% global open-risk budget,
the 2% daily drawdown lockout and the martingale prohibitions are unchanged — they are
what make that shape survivable. The count cap is removed only because the limit moves
to something stricter: every trade must clear its own transaction costs.

---

## A. Current bot diagnostic — why trade frequency is low

Six throttles, applied in series. Each is individually defensible; multiplied together
they produce a system that almost never trades.

### A.1 Structural throttles (code facts, measured by reading the code)

| # | Throttle | Where | Effect |
|---|---|---|---|
| 1 | Evaluation only on M5 close | `orchestrator._decision_loop`, `tf = Timeframe.M5` hardcoded | ≤288 evaluation instants per day, none mid-bar |
| 2 | Session gate | `config.session.allowed_sessions = [LONDON, NEW_YORK, OVERLAP]` | Asian and off-hours instants rejected before any setup logic runs |
| 3 | `min_rr = 2.0` | `g_min_rr`, a PLAN_GATE, never waived | Every plan targeting less than 1:2 is destroyed regardless of its win rate |
| 4 | Classification bar | `a_score_min 70` + `a_strong_categories_min 5`; A+ needs `85` + `7` | 7 of 10 scoring categories must be *strong* for A+ |
| 5 | `max_concurrent_positions = 1` | `g_exposure` | Never two positions, so one slow trade blocks the whole session |
| 6 | `max_trades_per_day = 3` | `g_trade_frequency` | Hard ceiling of 3 even if everything else passes |

Throttles 5 and 6 alone cap the system at 3 trades/day with no overlap. Nothing
downstream can exceed that.

### A.2 Measured throttles (synthetic data — provisional)

Instrumenting `sweep_mss_fvg` link by link over 12,000 synthetic M5 bars, 482 in-session
decision instants, 560 direction attempts:

| Link | Attempts killed | Note |
|---|---|---|
| 1 · HTF conflict (H4/D1/W1/MN1) | 280 | Structural: with a non-neutral HTF bias, one of the two directions always conflicts |
| 2 · recent, high-quality sweep | 74 | 20 none, 54 too old (>15 M15 bars) |
| 3 · displacement after sweep ≥0.5 ATR | 26 | |
| **4 · M15 MSS after the sweep, same direction** | **180** | **100% of everything that reached it** |
| 5 · entry FVG | **0 reached** | |
| 6–7 · dealing range, stop, targets | **0 reached** | |

**Caveat, stated plainly:** synthetic data is a random walk. An MSS requires a
directional bias established by a BOS and then broken *against* with ≥0.75 ATR of
displacement — a coordinated sequence a random walk does not produce in conjunction
with a liquidity sweep 15 bars earlier. So the 100% MSS kill rate is partly a property
of the data, not only of the filter. The session-gate and HTF-conflict figures are
structural and will hold on real data; the link 2–4 attribution must be re-measured on
harvested history before it is trusted. The harvest tooling now exists for exactly this.

### A.3 Which filters should remain HARD (for both engines)

Non-negotiable. These are safety, not selectivity:

`kill_switch` · `broker_connection` · `symbol_resolved` · `data_freshness` ·
`daily_drawdown` · `weekly_drawdown` · `monthly_drawdown` · `exposure` ·
`news_blackout` · `stop_validity` · `strategy_validated` · `no_duplicate` ·
`market_regime = ABNORMAL` (never tradable)

Plus three new hard gates:

- **net expectancy after costs** (§A.5) — scalp-only; the control that replaces the
  removed daily trade-count cap
- **correlation budget** (§D.3) — both engines; what makes ten concurrent positions ten
  bets rather than one leveraged bet
- **daily cost budget** (§D.4) — scalp-only; stops the day when accumulated friction
  outruns net progress

### A.4 Which filters should become SOFT — for the scalp layer only

The A/A+ engine keeps all of these as hard vetoes. The scalp engine scores them instead:

| Filter | A/A+ | Scalp | Rationale |
|---|---|---|---|
| `htf_conflict` D1 | hard veto | soft, weighted penalty | An intraday sweep-reversion legitimately trades against daily bias for 40 minutes |
| `htf_conflict` W1/MN1 | hard veto | **stays hard** | Trading against weekly structure is not a scalp, it is a mistake |
| `premium_discount` | hard veto | hard for reversal setups, soft for continuation | A continuation entry in premium is the setup, not a violation |
| `min_rr ≥ 2.0` | **unchanged** | replaced by net-expectancy gate | See §A.5 — this is the central change |
| M15 MSS | required | replaced by micro-MSS on M1/M5 | Same algorithm, different configuration |
| `session` | LONDON/NY/OVERLAP | per-setup, validated | Some setups may earn Asian-session eligibility; most will not |

### A.5 The central architectural change

Replace *"is the reward-to-risk ratio at least 2?"* with *"is expected value after real
costs positive by a meaningful margin?"*

```
net_expectancy_R = p_win × RR_net − (1 − p_win) × 1.0

RR_net = (stop × RR_gross − cost) / (stop + cost)
cost   = spread + slippage + commission, in price
```

This is **stricter** than `RR ≥ 2` in the way that matters — it can reject a 1:2 setup
whose costs eat it — and permissive in the way the objective requires. It is the
mechanism that makes small targets safe rather than merely allowed.

### A.6 Why this constraint dominates the whole design

Computed against the configured spec (contract 100 oz, `commission_per_lot = 7.0`,
`slippage_points_estimate = 15`, spread 25 points):

**Round-trip cost = $0.47 per ounce.**

| Stop | Cost as share of 1R | Net RR at 1:1 gross | Break-even win rate |
|---|---|---|---|
| 30 pts ($0.30) | **157%** | negative | impossible |
| 60 pts ($0.60) | 78% | 1:0.12 | **89.2%** |
| 100 pts ($1.00) | 47% | 1:0.36 | 73.5% |
| 200 pts ($2.00) | 24% | 1:0.62 | 61.8% |
| 300 pts ($3.00) | 16% | 1:0.73 | 57.8% |
| 500 pts ($5.00) | 9% | 1:0.83 | 54.7% |

At a 200-point stop, by gross target:

| Gross RR | Net RR | Break-even win rate |
|---|---|---|
| 1:1 | 1:0.62 | 61.8% |
| 1:1.25 | 1:0.82 | 54.9% |
| 1:1.5 | 1:1.02 | 49.4% |
| 1:1.75 | 1:1.23 | 44.9% |
| 1:2 | 1:1.43 | 41.2% |

**Three conclusions that shape everything below:**

1. **True M1-stop scalping is arithmetically dead on this instrument.** A 30-point stop
   costs more than it risks. Anyone promising tick-scalping on gold at these spreads is
   selling something.
2. **Stops must sit on M5/M15 structure — roughly $2–5 — with M1 used only for entry
   timing.** That keeps costs at 9–24% of 1R instead of 78–157%.
3. **The realistic holding window is 10–90 minutes, not seconds.** A $2–3 stop with a
   1:1.5 target needs a $3–4.5 move; gold covers that in tens of minutes during London
   and New York, not instantly. The engine is a *short-duration intraday* engine. That
   is still a large improvement on the current 48-bar (4-hour) time stop, and it is
   what the arithmetic supports.

The 70%-win-rate target interacts with this directly: 70% at 1:1 on a 60-point stop
**loses money** (89.2% needed). 70% at 1:1.5 on a 200-point stop is comfortably
profitable. The win rate is meaningless without the stop distance and the cost model
beside it.

### A.7 Components to leave untouched

`domain/` · `data/` (MarketView especially) · `intelligence/` · `execution/` · `ml/` ·
`monitoring/` · `risk/` internals · the four existing setups · `scoring.py` ·
`classifier.py` · `ENVIRONMENT_GATES` · the parity test.

Everything the scalp engine needs is added alongside.

---

## B. New scalp architecture

### B.1 Integration point

The scalp engine is a **second strategy family behind the same `DecisionPipeline`**, not
a second pipeline. It enters at `_detect`, where the registry already loops over
strategies, and diverges only at scoring, classification and risk tiering.

```
                        MarketView  (unchanged — the look-ahead boundary)
                             │
                     MarketAnalyzer.analyze()
                             │
              ┌──────────────┴───────────────┐
              │                              │
      MarketSnapshot                 MicroSnapshot   ← new, M1/M5, built from
      (existing, M5→MN1)             (new)             the SAME MarketView
              │                              │
     A/A+ strategies                 Scalp strategies
              │                              │
       ScoringEngine                  ScalpScorer     ← new, 0–100, own weights
              │                              │
        Classifier                    ScalpClassifier ← new, own tiers
       (A / A+ / NO_TRADE)            (SCALP / NO_TRADE)
              │                              │
              └──────────────┬───────────────┘
                             │
                      NetExpectancyGate   ← new, HARD, scalp-only
                             │
                        RiskGate          ← existing, extended with a scalp tier
                             │
                   CorrelationGate        ← new, HARD, both engines
                             │
                   ExecutionFilter / MT5  ← existing
```

`MicroSnapshot` is built from the same `MarketView` instance at the same instant. It
cannot see anything the A/A+ snapshot cannot — the look-ahead boundary is untouched and
remains the single source of truth.

### B.2 New modules

| Module | Purpose |
|---|---|
| `core/micro_structure.py` | Micro BOS/CHOCH/MSS, short-term swings, on M1/M5. **Reuses `StructureEngine` with a `MicroStructureConfig`** — no new algorithm |
| `data/micro_view.py` | `MicroSnapshot` assembly: micro structure, session liquidity, micro FVG/OB, VWAP, momentum |
| `strategy/scalp/` | Six setups (§C), each an independent module with its own enable switch |
| `strategy/scalp_score.py` | `ScalpScorer`, 0–100, weights in config, validated statistically |
| `strategy/scalp_gates.py` | `SCALP_HARD_GATES` incl. `g_net_expectancy`, `g_cost_ratio`, `g_micro_volatility` |
| `risk/cost_model.py` | Spread percentile, commission, modelled slippage → expected cost in price and in R |
| `risk/correlation.py` | Same-direction, same-zone, same-pool, same-setup-burst and aggregate exposure. **Load-bearing** — concurrency above 1 is gated on it (§D.3) |
| `risk/cost_budget.py` | Running daily friction total; halts the scalp tier when costs outrun net progress |
| `backtesting/holding_time.py` | Holding-time analytics and time-stop policy comparison |
| `backtesting/rr_sweep.py` | The 1:1 … 1:2 target sweep across setups, sessions and regimes |
| `dashboard/scalp.py` + panel | Scalp section and compounding simulator |

### B.3 Modules extended, not replaced

| Module | Extension |
|---|---|
| `core/analyzer.py` | Add M1 to `LTF`; add `bars_to_load["M1"]` |
| `domain/enums.py` | Add `Classification.SCALP`; add `Timeframe.M3` if the broker serves it |
| `risk/gate.py` | Add `Classification.SCALP → risk.risk_pct_scalp` to the existing risk map |
| `backtesting/metrics.py` | Add the time-based metrics (§E.4) alongside the existing ones |
| `engine/orchestrator.py` | Generalise `_decision_loop` from hardcoded M5 to a configured cadence per engine |
| `config/settings.py` | New `ScalpConfig`, `MicroStructureConfig`, `CostConfig` sections |

### B.4 Cadence

The A/A+ engine keeps its M5 cadence exactly. The scalp engine runs on **M1 close**,
evaluated only inside its enabled sessions. That is ~1,440 instants/day worst case,
~600 within London+NY. Measured cycle latency today is 180–515 ms, so an M1 cadence is
comfortable, but the loop must be restructured so a slow scalp cycle can never delay the
A/A+ cycle — separate tasks, shared snapshot cache, single writer to the journal.

---

## C. Strategy matrix

Six setups. Each has hard conditions (all must pass) and soft conditions (scored).
Every setup carries its own enable switch, its own validated RR band, its own session
eligibility and its own `ValidationStatus` — a setup that fails validation is disabled,
regardless of whether the system "needs more trades".

| Setup | Trigger chain | Hard conditions | Primary soft factors | Hypothesised regimes |
|---|---|---|---|---|
| **S1 · Liquidity sweep reversion** | session/intraday pool → sweep → rejection → micro-MSS → M1 FVG/OB entry | valid pool with ≥2 touches; rejection ratio ≥ cfg; micro-MSS after sweep; stop beyond sweep extreme; target = opposing intraday liquidity | HTF alignment, session, DXY, momentum divergence | RANGE, both trends, high vol |
| **S2 · FVG continuation** | H1/M15 bias → displacement → FVG → retrace → micro confirmation | established M15 bias; displacement ≥ cfg ATR; FVG unmitigated; entry in the gap | OB confluence, VWAP side, session | trends, high vol |
| **S3 · Order-block reaction** | impulse → validated OB → retrace into OB → micro confirmation | OB formed with BOS; untested; micro structure confirms; stop beyond OB | S/R confluence, HTF alignment | trends and range, high vol |
| **S4 · Breakout + retest** | intraday level (session H/L, PDH/PDL, equal highs) → break with displacement → retest → hold | level has ≥2 touches; break closes beyond by ≥ cfg ATR; retest holds on M1 | volume, momentum, session-open proximity | trends, high vol |
| **S5 · Momentum continuation** | strong displacement → shallow pullback → continuation trigger | ADX/regime threshold; pullback < 50% of impulse; no opposing HTF veto | VWAP, DXY, yields | STRONG_BULL / STRONG_BEAR only |
| **S6 · Session-open momentum** | London / NY open → opening range → break → continuation | inside the validated open window; opening range formed; spread normal | prior-session bias, macro alignment | **to be determined — assumed unprofitable until proven** |

The regime column is a **hypothesis to be tested, not a configuration to ship.** The
real matrix is generated by §E and written to config from the validation report — the
same way `allowed_regimes` and `allowed_sessions` are meant to be set today.

### C.1 Micro-structure model

Four distinct scales, explicitly named so nothing conflates them:

| Scale | Timeframes | Purpose |
|---|---|---|
| MACRO | MN1, W1 | Hard veto only |
| HIGHER | D1, H4 | Context, soft for scalps |
| INTRADAY | H1, M15 | Directional bias for continuation setups |
| MICRO | M5, M1 | Triggers, entries, stops |

`StructureEngine` is already config-driven, so MICRO reuses the identical algorithm with
a tighter configuration (`swing_lookback` 1, lower displacement thresholds). This is
deliberate: a second implementation of BOS/CHOCH/MSS would be a second thing to keep
correct, and the parity test only guards one.

---

## D. Risk model

Revised after the concurrency decision: **no daily trade-count cap, up to 10 concurrent
scalps, wins targeting 0.10–0.25% of capital.** The three caps that make that survivable
— the 2% global open-risk budget, the 2% daily drawdown lockout, and the martingale
prohibitions — are unchanged. Removing the count cap is only safe because the limit
moves somewhere stricter, not because it disappears.

### D.1 Tiering

| Tier | Risk per trade | Source |
|---|---|---|
| A+ | up to 2.00% | existing `risk_pct_a_plus` |
| A | 1.00% | existing `risk_pct_a` |
| **SCALP** | **0.15% (recommended default), configurable 0.10–0.25%** | new `risk_pct_scalp` |

`RiskGate.approved_risk_pct` already keys off `Classification`; adding `SCALP` to that
map inherits every existing cap.

### D.2 Why 0.15% is the recommended default

Concurrency and per-trade risk are the same dial, bounded by the unchanged 2% budget:

| Concurrent | @0.15% | @0.20% | @0.25% |
|---|---|---|---|
| 8 | 1.20% | 1.60% | 2.00% |
| **10** | **1.50%** | **2.00%** (at the cap) | 2.50% **breach** |
| 12 | 1.80% | 2.40% **breach** | 3.00% **breach** |

So: *10 concurrent at 0.20%*, or *8 at 0.25%*, or *10 at 0.15%* — never 10 at 0.25%.

The deciding argument is what a full losing cluster does:

| Risk each | 10 stop out together | Consequence |
|---|---|---|
| 0.15% | −1.50% | headroom left; the day continues |
| 0.20% | −2.00% | **instantly trips the daily lockout** |

At 0.20% a single bad cluster ends the trading day by construction. 0.15% keeps ten
positions open, lands wins at **0.153% of capital** — inside the requested 0.10–0.25%
band — and leaves the daily limit as a genuine backstop rather than a tripwire the
normal operating mode walks into.

Win size follows directly from risk at net RR 1.02 (a 1:1.5 gross target on a 200-point
stop, after costs):

| Risk | Typical win |
|---|---|
| 0.10% | 0.102% of capital |
| **0.15%** | **0.153%** |
| 0.20% | 0.204% |
| 0.25% | 0.255% |

A 0.25% win at 10 concurrent is only reachable by raising the gross target to 1:2, which
pulls against the smaller-targets objective. Reaching it with a small target means 8
concurrent, not 10. That trade-off is the operator's to make; both are configurable and
both are inside the cap.

### D.3 Correlation is the binding constraint, not the trade count

Ten XAUUSD positions are not ten bets. They are **one bet on gold, sized ten times.**
Monte Carlo over 200,000 clusters of 10 positions at 0.15–0.20% each, varying how alike
the signals are, reporting the probability that one cluster exhausts the entire 2% daily
loss limit:

| Signal correlation | 55% win rate | 65% | 75% |
|---|---|---|---|
| Independent (ρ = 0) | 0% | 0% | 0% |
| Realistic (ρ = 0.7) | **14.0%** | 8.4% | 4.5% |
| Same signal, same direction (ρ = 0.95) | **31.8%** | 23.1% | 14.9% |

The mean cluster return is positive in every row — this is not a claim that clusters
lose. It is that the variance is concentrated: at ρ = 0.7 the daily lockout fires
roughly **one day in seven**; at ρ = 0.95, one day in three. "Ten open constantly"
is self-limiting unless the ten are genuinely different trades.

**Therefore the correlation gate is load-bearing, not a refinement.** It is what converts
ten positions from one leveraged bet into ten bets, and it is a hard gate for both
engines:

- **Same direction** — aggregate same-direction scalp risk capped at a configured share
  of the global budget (proposed: 60%, so at most 6 of 10 face the same way)
- **Same zone** — reject a second entry whose stop sits within *k* × ATR of an open one
- **Same pool** — reject a second trade premised on the same liquidity pool
- **Same setup burst** — reject a second entry from the same setup within a configured
  window unless the premise differs
- **Strategy correlation** — measured from backtest returns; correlated setups share a
  sub-budget rather than each getting a full one

**Hard sequencing rule:** `max_concurrent_scalp` stays at **1** until the correlation
gate exists and is tested. Raising concurrency first would produce exactly the ρ = 0.95
row above.

### D.4 Frequency: the count cap is removed, the limit moves

`max_trades_per_day` no longer applies to the scalp tier. Per §10 of the brief, an
arbitrary count was never the right control. What replaces it — all binding
simultaneously:

| Control | Purpose |
|---|---|
| **Net-expectancy gate** (§A.5) | Every trade must clear its own costs. This is what makes unbounded frequency safe |
| Global open risk ≤ 2.0% | Unchanged. Caps concurrency automatically |
| `max_concurrent_scalp` = 10 | Explicit, and gated on D.3 |
| Correlation budgets | Prevents ten copies of one trade |
| Daily drawdown lockout 2% | Unchanged |
| **Daily cost budget** *(new)* | Stops the day when accumulated friction exceeds a set share of capital without net progress |
| `max_per_hour` / `max_per_session` | Retained as configurable safety valves, default **unlimited** |

### D.5 Why the count cap can go — and what would make that wrong

Each trade pays roughly 0.24R in costs at a 200-point stop. At 0.15% risk that is
**0.036% of capital per trade, win or lose**:

| Trades/day | Cost drag/day | Per 20-day month |
|---|---|---|
| 10 | 0.36% | 7.2% |
| 30 | 1.08% | 21.6% |
| 60 | 2.16% | 43.2% |
| 100 | 3.60% | 72.0% |

Frequency multiplies the edge and the friction **equally**. There is no advantage in the
count itself; a higher count is only better if each additional trade carries positive
net expectancy. The failure mode to guard against is not "too many trades" but "the
marginal trade's gross edge decays toward zero while its 0.24R cost does not" — which is
what the net-expectancy gate and the daily cost budget exist to catch.

### D.6 What follows *if* an edge exists

At 0.15% risk, 30 trades/day, net RR 1.02:

| Win rate | Edge/trade | Daily | Compounded over 20 days |
|---|---|---|---|
| 45% | −0.091R | −0.41% | −7.9% |
| 50% | +0.010R | +0.05% | +0.9% |
| 55% | +0.111R | +0.50% | +10.5% |
| 60% | +0.212R | +0.95% | +20.9% |
| 65% | +0.313R | +1.41% | +32.3% |
| 70% | +0.414R | +1.86% | +44.7% |

**This table is a consequence, not a forecast.** It states what would follow from a given
net win rate; it is not evidence that any setup achieves one. Whether these setups clear
55–65% *after costs, out-of-sample* is precisely the question §E exists to answer, and
the honest prior is that most will not. Note also how narrow the margin is: 50% is
indistinguishable from noise and 45% compounds downward at the same speed 55% compounds
up. At this frequency the system is a machine for expressing whatever edge exists,
amplified — in both directions.

### D.7 Compounding

Sizing already derives from live equity via `AccountState`, so compounding works today
and needs no change. The invariant to encode as a test: **the risk percentage is
constant and never a function of recent results.** An account that doubles trades
0.15% of the larger balance; an account in drawdown trades 0.15% of the smaller one.

Martingale, grid, averaging down, increasing risk after losses, revenge trading and
stop-widening remain prohibited for the scalp tier exactly as for A/A+. Each gets a test
asserting it is *impossible*, per the project's testing convention — the higher trade
count makes these more dangerous, not less.

## E. Backtest plan — how each setup is proved or discarded

### E.1 Prerequisite

Real harvested M5/M1 history. Synthetic data cannot validate any of this; the pipeline
suite asserts it produces no trades. Target: ≥2 years of M1 where the broker serves it,
falling back to M5 with M1 only for the most recent window.

### E.2 Split discipline

```
TRAIN (50%) → VALIDATION (20%) → OUT-OF-SAMPLE (30%, touched once)
        ↓
   WALK-FORWARD (rolling, purged)
        ↓
   MONTE CARLO (trade-order and slippage resampling)
```

`backtesting/walk_forward.py`, `monte_carlo.py` and `validation.py` already exist and
are reused unchanged. `ml/purged_cv.py` provides the purged splits.

### E.3 The RR sweep

For each setup × session × regime, sweep gross targets 1:1, 1:1.25, 1:1.5, 1:1.75, 1:2
and stop placements (structural, structural + buffer, ATR-scaled). Report **net**
figures at three cost scenarios: optimistic (15 pts spread, 5 slippage), expected (25/15,
the configured default), pessimistic (50/15).

A configuration ships only if it is profitable in the **pessimistic** column. A
configuration that only works at optimistic costs is a backtest artefact.

### E.4 Metrics added

Per trade: holding time (minutes), MAE/MFE (already present), realised vs planned RR,
cost as a share of 1R.

Aggregate: average / median / 90th-percentile / max holding time · profit per hour ·
expectancy per hour · win rate by holding-time bucket · profit factor by holding-time
bucket · performance by session · performance by target size · gross vs net expectancy ·
spread cost · commission · slippage · risk of ruin (already present).

### E.5 Time-stop policy

For each setup, compare four policies over the same trades — hold to resolution, close
at the p90 holding time, reduce at p90 and hold the remainder, move to break-even at
p50 — and pick per setup by net expectancy per hour, not by preference.

### E.6 Acceptance criteria

A setup is promoted from `DEV` only when **all** hold:

1. Out-of-sample net expectancy > 0 at pessimistic costs
2. Walk-forward: net-positive in ≥70% of windows, no window worse than −6R
3. Monte Carlo: 5th-percentile terminal equity above starting equity; risk of ruin < 1%.
   **Resampling must preserve the correlation structure** — see §E.7
4. Profit factor ≥ 1.3 net, out-of-sample
5. Return/max-drawdown ≥ 2.0
6. ≥200 out-of-sample trades (otherwise the win-rate confidence interval is too wide to
   act on — the existing Wilson interval is already computed and is the check)
7. Median holding time within the setup's declared window
8. **Cluster drawdown**: with the correlation gate active and concurrency at 10, the
   observed frequency of days hitting the 2% lockout must be below a configured
   threshold (proposed 10% of trading days). §D.3 shows this is the failure mode that
   concurrency introduces, so it is measured directly rather than inferred

Win rate is **recorded and reported at every stage but is not itself an acceptance
criterion.** 70% remains the stated aspiration; a setup at 62% with strong net
expectancy and a stable walk-forward passes, and one at 74% with negative net
expectancy after costs fails. That ordering is deliberate and follows §30 of the brief.

### E.7 Correlation-aware resampling

Standard Monte Carlo shuffles trades independently. That is wrong here and would
flatter the result: §D.3 shows independent trades never hit the daily lockout while
realistically correlated ones do so on 14% of days. Independent resampling would
therefore report a risk of ruin near zero for a system whose actual failure mode it had
resampled away.

Three changes:

1. **Resample clusters, not trades.** The unit is a set of positions open at the same
   time, kept intact.
2. **Sweep the correlation assumption** at ρ = 0, 0.7 and 0.95 and report all three.
   Ship on the ρ = 0.95 column, the same way the cost sweep ships on the pessimistic
   column.
3. **Block-bootstrap by day** so intraday clustering and same-session regime persistence
   survive the resampling.

The correlation gate's job is to move the *realised* ρ down. §E measures what it
actually achieves rather than assuming it works.

### E.8 Deployment gate

The existing Phase 10 gate is extended, not bypassed. `LIVE_TRADING` stays `false`; each
scalp setup carries its own `ValidationStatus`; live routing requires `OOS_PASSED` per
setup, exactly as the swing strategies do today. Demo → paper → small live remains
mandatory and is wall-clock work.

---

## F. Implementation plan

Ten stages. Each ends green — tests, lint, types — and each is independently revertible.
Nothing after stage 1 is written before stage 1's numbers exist.

| Stage | Work | Exit condition |
|---|---|---|
| **0** | Harvest ≥2 years of real M5 (+M1) history | `xauusd harvest` holds the target span; coverage reported |
| **1** | Re-run the funnel diagnostic on **real** data | The §A.2 attribution is re-measured and this spec is corrected where synthetic data misled it |
| **2** | `risk/cost_model.py` + `g_net_expectancy` + `g_cost_ratio`, with tests | Cost gate provably rejects the 30-point-stop case and accepts the 300-point one |
| **3** | Time-based metrics in `backtesting/metrics.py` | Holding time, profit/hour, expectancy/hour computed and tested against hand-worked fixtures |
| **4** | `MicroStructureConfig` + M1 in the analyzer + `MicroSnapshot` | Micro BOS/CHOCH/MSS detected on real M1; MarketView boundary test still passes |
| **5** | Setups S1–S3 behind an `enabled_scalp_strategies` switch, default **off** | Each emits candidates on real data; per-setup funnel measured |
| **6** | `ScalpScorer` + `Classification.SCALP` + scalp risk tier at 0.15%, `max_concurrent_scalp` pinned to 1 | Risk map extended; every existing risk test still passes; open-risk cap provably still binding |
| **7** | `risk/correlation.py` + daily cost budget + per-tier frequency ladder | Same-zone, same-pool and same-burst duplicates provably impossible; **only now may `max_concurrent_scalp` rise above 1** |
| **8** | RR sweep + walk-forward + Monte Carlo across S1–S3 | The real strategy-regime matrix is generated and written to config |
| **9** | Setups S4–S6, same treatment | Promoted only on their own merits |
| **10** | Dashboard scalp panel + compounding simulator | Live signals, per-setup stats, simulator clearly labelled a simulation |

Stages 0–3 are worth doing regardless of whether the scalp engine ships: they make the
existing engine measurable, and stage 2's cost model improves the A/A+ engine too.

### F.1 Decisions taken

| Question | Decision | Where |
|---|---|---|
| Daily trade-count cap | **Removed** for the scalp tier; replaced by the net-expectancy gate, the open-risk cap and the daily cost budget | §D.4 |
| `max_concurrent_scalp` | **10**, pinned to 1 until the correlation gate ships | §D.2, §D.3 |
| `risk_pct_scalp` | **0.15%** default (0.10–0.25% configurable) — 10 open = 1.50%, wins ≈ 0.153% | §D.2 |
| Win size | 0.10–0.25% of capital, achieved by the risk dial rather than by target inflation | §D.2 |
| Global 2% open-risk cap | **Unchanged** | §D |
| 2% daily drawdown lockout | **Unchanged** | §D |

### F.2 Still open

1. **Holding-time expectation.** The arithmetic says 10–90 minutes, not seconds. If the
   intent was true tick-scalping, the spread makes that impossible on this instrument
   and the plan should be reconsidered rather than built.
2. **Session scope.** Whether to test Asian-session eligibility at all, given spreads are
   typically widest there and §A.6 shows how punishing that is.
3. **0.25% wins at 10 concurrent.** Only reachable at a 1:2 gross target, which pulls
   against smaller targets. The alternative is 8 concurrent at 0.25%. Either is inside
   the cap; the choice is the operator's.
