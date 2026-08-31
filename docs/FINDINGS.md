# Findings

Bugs and design problems found while building this system, and what each would have
cost if it had reached production. Recorded because the *class* of each bug is more
useful than the fix — every one is a mistake that is easy to make again.

---

## 1. Sortino inflated ~120× (most serious)

**Found by:** rendering the dashboard against real data and asking whether "Sortino
700.05" could possibly be true.

Sortino used the standard deviation of the **losing subset** as its denominator, rather
than the textbook downside deviation `rms(min(r − target, 0))` over **all** trades.

For a fixed-stop system this is not a rounding difference. Every loss is approximately
−1R by construction, so the losing subset has almost no dispersion:

```
real 12-trade sample:
  std(losses)          = 0.005   →  Sortino 700
  downside deviation   = 0.662   →  Sortino 5.2
```

**Cost if shipped:** the deployment gate requires Sortino ≥ 2.0. Every fixed-stop
strategy would have passed that criterion trivially and meaninglessly, including ones
the correct formula blocks. This is precisely the failure the gate exists to prevent.

**Class of bug:** a statistic that is *defined* for a distribution the system does not
produce. Worth re-examining every ratio whose denominator is a dispersion measure.

---

## 2. Every exit labelled `STOP_LOSS`

**Found by:** an 11-month backtest whose exit-reason breakdown was
`Counter({'STOP_LOSS': 5})` — including a trade that closed at **+1.03R**.

`SimBroker` labelled any exit at the stop price a stop-out, without checking whether the
stop had been *moved* to break-even or trailed into profit.

**Cost if shipped:** the exit-reason analytics would be useless, and worse, actively
misleading — a reviewer would conclude the system never reaches its targets. Since
break-even and trailing behaviour is exactly what one tunes from that breakdown, it
would have been tuned blind.

The fix uses a **risk-relative** tolerance rather than exact equality: the entry sits on
the far side of the spread, so an exact comparison never matches and everything stays
mislabelled.

---

## 3. `planned_rr` computed the realised move

`ClosedTrade.planned_rr` returned `|exit − entry| / risk` — the distance price actually
travelled, not the RR the plan targeted. It feeds `avg_rr_planned` in validation reports.

**Cost if shipped:** a system with a hard 1:2 floor reporting "planned RR 0.98". An
obviously impossible number, which is the good case; the bad case is nobody noticing and
the metric quietly meaning nothing. Split into `planned_rr` (from the plan) and
`realised_rr` (travelled), with both plus the payoff ratio shown separately.

---

## 4. A directional bias from 25 bars

The structure engine's minimum-history guard was `atr_period + 2·lookback + 2` = 20 bars.
With 25 bars of noise it would return `BULLISH` and that bias would propagate into the
HTF-alignment gate as though it were knowledge.

Raised to `max(3 × atr_period, 50)`. **Class of bug:** a guard that ensures a *calculation*
is defined, mistaken for one that ensures the *answer* is meaningful.

---

## 5. Scoring weights totalled 95, not 100

The example weights in the original brief sum to 95. Left alone, every score would have
been silently capped at 95 and thresholds calibrated against a scale nobody intended.
Caught by a validator that now enforces the total, so the error cannot recur.

---

## 6. A plan at exactly 1:2 can never fill

A setup planned at exactly the 1:2 floor is *always* rejected once the spread is applied,
because the fill happens on the far side of the quote.

Not a bug — correct behaviour — but a non-obvious consequence worth knowing: **strategies
must target above the floor, not at it.** Kept as an explicitly named test rather than
worked around.

---

## 7. Strategies emitted candidates the classifier always rejected

Strategies checked D1/W1/MN1 for higher-timeframe conflict; the classifier checked
MN/W/D/**H4**. Every H4-conflicting candidate was generated, scored, gated and rejected.

Wasted work, and worse, it polluted the rejection ledger — the screen used to diagnose
whether the bot is idle by design or by defect.

---

## 8. Performance: the decision cycle was 434 ms

Three separate problems, all found by profiling rather than guessing:

| Problem | Fix | Effect |
|---|---|---|
| `series.body_ratio` recomputed the whole array inside a per-bar loop | hoist | |
| Swing detection was a Python loop of `np.all` calls | sliding-window vectorisation | |
| Zone analysis re-ran every M5 bar though it only changes on an M15 close | cache on the setup bar | |
| | **combined** | **434 ms → 25 ms** |

---

## 9. LightGBM thrashes under container CPU limits

LightGBM defaults to one thread per core. Under a cgroup CPU limit (a VPS, or CI) this
causes severe thread contention: **42.7 seconds** for a 600-sample fit versus **0.02
seconds** with `n_jobs` pinned. Pinned deliberately, with the measurement recorded in the
code so nobody "optimises" it back.

---

## 10. Conventional red/green fails colourblind readers

The standard trading green/red pair measures ΔE 4.1 under deuteranopia — effectively
indistinguishable for roughly one man in twelve. Measured with a palette validator rather
than eyeballed, then replaced with a teal/coral pair measuring 11.8 that still reads as
up/down. Colour is never the only cue regardless: every direction carries a text label.

---

## 11. Swallowed dashboard errors

Every dashboard refresh had `catch (e) { /* ignore */ }`. A failed fetch produced an
empty panel, which is indistinguishable from "no data" — on a trading dashboard, the
difference between "the bot is being selective" and "the bot is broken". Failures now
render in the panel where the operator will see them.


---

## 12. A test that bypassed the gates it was meant to exercise

The trade-path integration test passed `gates_passed=True` into the classifier so it
could focus on scoring. That silently stopped checking every requirement enforced by a
**gate** rather than by the classifier — premium/discount location among them.

It surfaced only because a deliberately-weakened case (a long in premium) was still
classified `A`. The classifier is right not to duplicate the gate; the test was wrong to
stub it. It now runs `run_gates` for real.

**Class of bug:** a test helper that simplifies away the exact composition it is
supposed to be testing. Worth asking of any test fixture: which real component did I
replace with `True`?

---

## 13. Generating data and hoping it contains a setup

The original version of that test generated a random walk and asserted at least one
A-grade trade appeared. It failed repeatedly — not because the decision path was broken,
but because a random walk rarely contains the full confluence chain, which is the whole
point of the system.

Three attempts to hand-plant a setup were each *correctly* rejected: the first because a
14-point 5-minute displacement is roughly 15 ATR and the regime engine classified the
market ABNORMAL; the second because the retrace landed in premium; the third because the
deep pullback made H1 read bearish, conflicting with a long.

That difficulty is itself informative — it is a fair proxy for how rare a genuine A setup
is meant to be. The fix was to stop conflating two questions:

- *Does the decision path turn a valid setup into a sized trade?* → construct the
  snapshot directly. Fast, deterministic, and it tests the actual claim.
- *Does real market data contain such setups?* → the backtester answers this, and its
  answer was 5 trades from 24,064 evaluations over 11 months.
