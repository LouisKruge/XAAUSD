# Spec — Liquidity, FVG, Order Blocks, S/R

Companion to `market_structure.md`. Same principle: objective definitions, no
subjective interpretation, nothing knowable only in hindsight.

---

## 1. Liquidity pools

A pool is a price level where stop orders accumulate. Kinds tracked:

| Kind | Definition |
|---|---|
| `BSL` / `SSL` | An untaken structural swing high / low |
| `EQH` / `EQL` | ≥ `min_equal_touches` extremes within `equal_level_tolerance_atr` × ATR |
| `PDH` / `PDL` | Previous **closed** daily bar's high / low |
| `PWH` / `PWL` | Previous **closed** weekly bar's high / low |
| `SESSION_HIGH/LOW` | Extremes of the Asian session, the reference for the London open |
| `RANGE_HIGH/LOW` | Boundaries of the current dealing range |

**Tolerance is ATR-relative, not a fixed number of points.** Gold's volatility differs
by an order of magnitude between 2015 and 2025; a fixed 20-point tolerance would mean
completely different things in each era.

A pool is **resting** until price trades through it, then **swept**. Only resting pools
are valid take-profit anchors.

---

## 2. Sweeps

A sweep is a penetration beyond a pool followed by rejection back through it. ALL of:

1. `penetration >= sweep_min_penetration_atr × ATR` — it actually took the stops
2. `penetration <= sweep_max_penetration_atr × ATR` — a deep move through is a genuine
   **break**, not a sweep; without this cap every breakout is misread as a stop hunt
3. `rejection_ratio >= sweep_min_rejection_ratio` where rejection is the wick beyond the
   pool as a fraction of the bar's range
4. close back inside the pool, within `sweep_max_bars_to_reject` bars
5. `displacement_after >= displacement_after_sweep_atr × ATR` in the reversal direction

Quality is a 0–1 composite of penetration depth, rejection strength, speed of rejection,
and displacement afterwards, halved if price never closed back inside.

> **A sweep is never, on its own, a trade signal.** The liquidity engine answers
> "was liquidity taken, and how convincingly". It has no opinion about trading.

**Stop hunt** = a sweep with high rejection and fast reversal.
**False breakout** = a level broken by a body close and reclaimed within N bars.

---

## 3. Fair value gaps

Three-bar imbalance:

```
bullish FVG at bar i:   low[i+1]  > high[i-1]     gap = (high[i-1], low[i+1])
bearish FVG at bar i:   high[i+1] < low[i-1]      gap = (high[i+1], low[i-1])
```

Required, not merely scored:

- `size >= min_size_atr × ATR`
- **`displacement (body of bar i) >= min_displacement_atr × ATR`** — a gap left by three
  small drifting bars is not an institutional footprint

Lifecycle:

```
UNMITIGATED → PARTIAL → MITIGATED → INVALIDATED (fully traded through)
                     ↘ INVERTED (traded through, now acting as the opposite)
```

Entry is **consequent encroachment** (the 50% level) by default: materially better fill
than the near edge, materially more likely to be reached than the far edge.

Quality (0–1) weights displacement 0.30, size 0.15, state 0.20, premium/discount
location 0.15, order-block confluence 0.10, swept liquidity 0.05, HTF alignment 0.05.

---

## 4. Order blocks

The last opposing candle before a displacement **that broke structure**.

| Kind | Definition |
|---|---|
| `BULL_OB` | Last DOWN candle before bullish displacement that broke structure |
| `BEAR_OB` | Last UP candle before bearish displacement that broke structure |
| `BULL_BREAKER` | A failed bearish OB, now acting as support |
| `BEAR_BREAKER` | A failed bullish OB, now acting as resistance |

`require_bos` defaults **true** and should stay true. Without a resulting BOS/MSS, "the
last down candle before an up move" is just a candle; relaxing it roughly triples the
zone count and destroys their meaning.

Zone boundaries use the candle's **wick extremes** by default (`use_wick_extremes`),
because the wick is where the orders actually filled.

Invalidated by a **body close** through the far side. Fresh → Tested → Mitigated after
`max_tests_before_stale` touches.

---

## 5. Support, resistance, supply, demand

Swing extremes clustered within `cluster_tolerance_atr` × ATR, requiring at least
`min_touches` members. Importance is:

```
importance = tf_weight × (0.35 × min(touches/4, 1)
                        + 0.35 × rejection_strength
                        + 0.30 × recency)
```

`tf_weight`: MN1 1.00, W1 0.90, D1 0.80, H4 0.60, H1 0.40, M15 0.22, M5 0.10.
`rejection_strength` is the average move away from the level over the following 10 bars,
in ATR, normalised. `recency` decays exponentially with `recency_halflife_bars`.

A level with `importance >= 0.45` sitting between entry and a candidate target
**disqualifies that target** — this is what stops the system placing a 1:3 target on the
far side of a daily level that has held four times.

---

## 6. Premium and discount

From the dealing range (`market_structure.md` §9):

```
position = (price − range_low) / (range_high − range_low)
```

| Position | Label |
|---|---|
| < 0.25 | DEEP_DISCOUNT |
| 0.25 – 0.50 | DISCOUNT |
| 0.50 – 0.75 | PREMIUM |
| > 0.75 | DEEP_PREMIUM |

Longs belong in discount, shorts in premium. The gate permits a small overshoot
(long up to 0.60, short down to 0.40) because a hard boundary at exactly 0.50 rejects
good setups on a rounding error. Beyond that the trade is blocked outright.

An unknown dealing range does **not** veto; the scoring engine handles the uncertainty
by awarding nothing for location.
