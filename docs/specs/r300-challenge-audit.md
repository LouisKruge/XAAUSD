# R300 → R10,000 challenge — audit and viability verdict

Audit first, per the brief. No strategy code was modified. One safety check was added
(§9), because it is what the brief's §41 mandates and it protects the account
immediately.

---

## Verdict, up front

**R300 is not executionally viable for XAUUSD on this broker.** The account is short by
roughly **81×** for the risk model already specified, and by **24×** even at a
much more aggressive 1% per trade.

This is not a judgement about the strategy. It is arithmetic about the broker's minimum
lot, and no amount of strategy quality changes it.

---

## A. Existing architecture audit

### A.1 What the audit can and cannot report

The brief asks for win rate, profit factor, expectancy, maximum drawdown, consecutive
losses, average holding time, average TP/SL distance, realised R:R, per-session and
per-strategy performance, and counts of rejected-but-profitable setups.

**None of these exist, and reporting numbers for them would be fabrication.** The
reasons, in order of severity:

| # | Fact | Consequence |
|---|---|---|
| 1 | No real price history has ever been loaded | Every backtest to date ran on synthetic random-walk data |
| 2 | The suite asserts synthetic data yields zero trades (`test_no_edge_data_produces_no_trades`) | The only runnable backtest was guaranteed to produce nothing |
| 3 | The system has never taken a trade — live, demo or backtest | There is no trade population to compute statistics over |
| 4 | Every strategy sits at `ValidationStatus.DEV` | Nothing has passed out-of-sample validation |

So the honest answer to "what is the current win rate / profit factor / expectancy" is
**unknown, and unknowable until real history is harvested.** The harvest tooling now
exists; that is stage 0. Anything else printed in those fields would be invented.

### A.2 What the audit *can* report

Structural facts, read from the code and measured:

| Finding | Detail |
|---|---|
| Evaluation cadence | `_decision_loop` hardcodes M5 — ≤288 instants/day, never mid-bar |
| Session gate | LONDON/NEW_YORK/OVERLAP only; ~42% of instants rejected before setup logic |
| `min_rr` | 2.0, a hard plan gate, never waived |
| Classification bar | A: score ≥70 + 5 strong categories · A+: ≥85 + 7 of 10 |
| `max_concurrent_positions` | **1** |
| `max_trades_per_day` | **3** |
| Candidate funnel | 560 direction attempts → 0 plans; the M15 MSS link killed 100% of the 180 that reached it, and links 5–7 were never evaluated |
| Round-trip cost | **$0.47/oz** at spread 25 + slippage 15 + $7/lot commission |

The funnel figure is from synthetic data and is flagged provisional — a random walk
cannot produce an MSS coordinated with a sweep. Full detail in
`docs/specs/scalp-engine.md` §A.

### A.3 What is already correct and must not be "fixed"

The brief's §13 (micro-account handling) **is already implemented**, and correctly.
`PositionSizer` computes the theoretical size, floors to the volume step — never rounds
up — and refuses outright when the result is below `volume_min`:

> `required size 0.0012 lots is below the broker minimum 0.01; the account is too small
> for a structural stop of 2.00 at 0.15% risk`

It never shrinks the stop to make a position fit. A final invariant then re-checks
realised risk against the cap and trips the kill switch on violation. No change needed.

---

## B. Why trade frequency is low

Six throttles in series, detailed in `docs/specs/scalp-engine.md` §A. The two that bind
hardest are `max_concurrent_positions = 1` and `max_trades_per_day = 3`: together they
cap the system at three non-overlapping trades a day regardless of opportunity.

**On a R300 account there is a seventh throttle that overrides all of them**, and it is
absolute — see §C.

---

## C. The R300 reality check (brief §41)

### C.1 What one minimum lot actually risks

Broker spec, as reported by the terminal and verified in pre-flight: contract 100 oz,
minimum volume 0.01, tick size 0.01, tick value 1.0. So **0.01 lot = 1 ounce**, and a
$1.00 move in gold is $1.00 of P&L.

R300 ≈ $16.48 at R18.2/USD.

| Structural stop | Risk on 0.01 lot | As % of R300 |
|---|---|---|
| $0.30 (30 pts) | $0.30 | 1.8% |
| $0.60 (60 pts) | $0.60 | 3.6% |
| $1.00 (100 pts) | $1.00 | 6.1% |
| **$2.00 (200 pts)** | **$2.00** | **12.1%** |
| $3.00 (300 pts) | $3.00 | 18.2% |
| $5.00 (500 pts) | $5.00 | 30.3% |

The configured scalp budget is 0.15–0.50% per trade. The smallest structurally
meaningful stop is **20–80× that**.

### C.2 The reductio

What stop distance *would* fit the risk budget?

| Risk budget | Largest stop that fits |
|---|---|
| 0.15% | **2.5 points** ($0.025) |
| 0.50% | 8.2 points ($0.082) |
| 2.00% | 33.0 points ($0.330) |

The spread alone is ~25 points. The round-trip cost is 47 points. **Every stop in that
table is inside the transaction cost** — the trade is underwater the instant it opens.

There is therefore no stop distance at which a R300 account both fits its risk budget
and clears its costs. The constraint is not tight; it is unsatisfiable.

### C.3 Margin

0.01 lot of gold at $2,600 is $2,600 of notional.

| Leverage | Margin per min lot | Share of R300 | 10 concurrent |
|---|---|---|---|
| 1:100 | $26.00 | 158% | 1,577% |
| 1:500 | $5.20 | 31.5% | 315% |
| 1:1000 | $2.60 | 15.8% | 158% |

Even at 1:1000, the ten-concurrent design cannot be held. At 1:100 a *single* minimum
position exceeds the account.

### C.4 Minimum viable capital

At a $2.00 structural stop:

| Risk per trade | Minimum equity | In Rand |
|---|---|---|
| 0.15% (the specced scalp tier) | $1,333 | **R24,300** |
| 0.50% | $400 | **R7,300** |
| 1.00% (the A tier) | $200 | R3,600 |
| 2.00% (A+ only, ceiling) | $100 | R1,800 |

For the ten-concurrent design the budget must cover ten minimum lots: **≈$14,000
(R255,000)** at 0.15%.

R1,800 is the absolute floor at which *one* trade can be placed without breaching the
2% ceiling — and an account operating permanently at its A+ maximum has no risk
headroom at all. It is a floor, not a recommendation.

---

## D. Probability of reaching R10,000, and of ruin (brief §S, §T)

If the minimum-lot risk were accepted anyway — 12.1% per trade, fixed-fractional, $2.00
stop, net RR 1.02 — Monte Carlo over 20,000 paths of up to 5,000 trades each:

| Net win rate | Reached R10,000 | Blew up | |
|---|---|---|---|
| 45% | 0.0% | **100.0%** | no edge |
| 50% | 0.1% | 83.2% | coin flip after costs |
| 52% | 29.2% | 46.5% | marginal edge |
| 55% | 81.5% | 18.5% | the optimistic case |
| 60% | 96.3% | 3.7% | strong |
| 65% | 99.3% | 0.7% | exceptional |

**Read that table as a warning, not an encouragement.** The 81.5% figure at 55% is
seductive and it is doing no work: the win rate is the input, and it has never been
measured. The stress tests the brief asks for (§40) show how sharp the edge of the
cliff is:

| Scenario | Reached R10,000 | Blew up |
|---|---|---|
| Baseline 55% | 81.3% | 18.7% |
| **Win rate falls 10% (55 → 49.5)** | **0.0%** | **91.4%** |
| Average win shrinks 20% | 0.0% | 91.5% |
| Slippage doubles | 29.2% | 43.2% |

A 5.5 percentage-point change in an unmeasured parameter moves the outcome from
"81% success" to "certain ruin". That is not a strategy; it is a coin weighted by a
number nobody has observed.

**Honest answer to the brief's §S and §T: the probability of reaching R10,000 from R300
cannot be stated, because it depends almost entirely on a win rate that has never been
measured, and the sensitivity to that parameter is extreme. What can be stated is that
at any win rate below ~52% net, ruin is the overwhelmingly likely outcome.**

---

## E. Recommended path

Three options, in the order I would rank them.

### E.1 Fund the account to the design (recommended)

**R7,300** enables the specced engine at 0.50% risk per trade with one position at a
time. **R24,300** enables the 0.15% tier the scalp spec is written around. Everything
already designed then applies unchanged, and the challenge becomes a genuine test of
whether the strategy has an edge rather than a test of whether a coin lands right eight
times.

### E.2 Find an instrument the account can actually trade

Some brokers offer gold in smaller units — nano lots (0.001), or a 1-ounce cash CFD.
A 0.001 minimum would divide every figure in §C.1 by ten, putting a $2.00 stop at 1.2%
of R300 — still four to eight times the target budget, but no longer absurd. Worth
checking on the current broker before assuming it is unavailable; the pre-flight now
prints `volume_min` explicitly.

This does not fix the cost arithmetic: at a 200-point stop, costs remain 24% of 1R
regardless of account size.

### E.3 Run the challenge on demo first

Costs nothing, produces the trade population that §A.1 says does not exist, and turns
the win rate from an assumption into a measurement. Given §D, this is the only way the
R300 question becomes answerable rather than speculative.

**What I would not do** is proceed at 12% risk per trade. It is not a challenge with
aggressive risk settings; it is eight consecutive losses away from zero, at a loss rate
we have no evidence about.

---

## F–R. The rest of the requested deliverables

The engine design the brief asks for in D through M — scalp architecture, setup
specifications, risk engine, compounding, execution, news, regime, backtesting, Monte
Carlo, dashboard — is already written in `docs/specs/scalp-engine.md`, revised for the
ten-concurrent operating shape. It is unchanged by this audit except that its minimum
account size is now stated explicitly (§C.4).

The challenge-specific additions the brief introduces — the stage machine (§19), equity
milestones (§18), and the progression risk schedule — are deferred rather than designed,
for one reason: **a stage machine that moves an account from R300 to R10,000 is
meaningless while the first stage cannot place a trade.** They become worth specifying
once §E resolves the capital question, and they are straightforward once it does.

---

## G. What was built

`src/xauusd/risk/viability.py` — `assess_account()` and `ViabilityReport`, wired into
`xauusd doctor` and covered by 13 tests.

The sizer already refused sub-minimum positions correctly, per trade. What was missing
was the account-level statement: on an account too small for the instrument, *every*
setup is refused for one structural reason, and what the operator sees is a bot that
never trades — indistinguishable from a strategy being selective. That confusion has
already been the most expensive one in this project.

Pre-flight now says it once, before any money is committed:

```
account viability:
  equity           : 16.48 USD
  risk budget      : 0.15% per trade
  structural stop  : 2.00 price
  minimum lot risk : 2.00 USD = 12.1% of equity
  margin (min lot) : 5.20 USD = 31.6% of equity
  VERDICT          : ACCOUNT NOT EXECUTIONALLY VIABLE UNDER CURRENT BROKER CONDITIONS
                     one minimum lot (12.1%) exceeds the 0.15% budget by 81x
                     minimum viable equity at this stop and risk: 1,333.33 USD (81x current)
                     every trade will be refused by the position sizer. That is the sizer
                     working, not a strategy that is being too selective.
```

`doctor` exits non-zero on that verdict, so it fails pre-flight rather than starting a
bot that would reject every setup in silence.
