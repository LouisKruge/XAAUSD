# Spec — Market Structure Engine

Objective definitions. No subjective interpretation, no discretionary judgement, and
above all no look-ahead: a structural fact is only usable from the bar at which it
became knowable.

## 1. Swing points

A **swing high** at index `i` requires, with lookback `L` (default 2):

```
high[i] > high[j]   for all j in [i-L, i-1]      (strictly greater to the left)
high[i] >= high[j]  for all j in [i+1, i+L]      (greater-or-equal to the right)
```

A **swing low** is the mirror image on lows.

Two consequences that matter:

* **Confirmation lag.** The swing at `i` is not knowable until bar `i+L` has closed.
  Every swing therefore carries `confirmed_index = i + L`, and the engine only ever
  considers swings with `confirmed_index <= current_index`. Without this the whole
  system silently sees the future, because a fractal is defined by bars after it.
* **Asymmetric comparison.** Strict on the left, non-strict on the right. This makes
  the definition deterministic on flat tops (equal highs), which is precisely where
  liquidity accumulates and where an ambiguous rule would matter most.

### Significance filter

A swing is **structural** only if its leg from the previous opposite swing is at least
`swing_min_atr` × ATR(14) (default 0.25). Smaller swings are recorded as **internal**
structure and never used for a BOS on the same timeframe.

### Alternation

The swing sequence must alternate high/low. When two consecutive swings share a kind,
the more extreme one is kept. This prevents a cluster of noise highs from being read
as a sequence of lower highs.

## 2. Labels

Given consecutive same-kind structural swings:

| Condition | Label |
|---|---|
| `high[n] > high[n-1]` | HH |
| `high[n] < high[n-1]` | LH |
| `low[n] > low[n-1]` | HL |
| `low[n] < low[n-1]` | LL |

## 3. Break of Structure (BOS)

A **bullish BOS** at bar `k` requires ALL of:

1. `close[k] > swing_high.price` — a **body close** beyond the level, not a wick.
   (Configurable via `bos_require_body_close`; a wick through a level is a raid, not
   an acceptance, and treating the two the same is the single most common source of
   false structure.)
2. `swing_high.confirmed_index <= k` — the level was knowable at the time.
3. `displacement >= bos_min_displacement_atr × ATR` where
   `displacement = close[k] - swing_high.price`.
4. `body_ratio[k] >= bos_min_body_ratio` (default 0.5) — the breaking candle is a
   decisive one, not a doji that happens to close a cent through.
5. The prevailing bias was already bullish, or neutral. A break in the direction of the
   existing trend is a BOS; a break against it is a CHOCH (below).

Bearish BOS is the mirror.

## 4. Change of Character (CHOCH)

The **first** structural break AGAINST the prevailing bias.

* In a bullish structure (making HHs and HLs), a body close **below the most recent
  structural higher low** is a bearish CHOCH.
* In a bearish structure, a body close **above the most recent structural lower high**
  is a bullish CHOCH.

Requirements 1–4 of BOS apply identically. A CHOCH flips the working bias to NEUTRAL —
not to the opposite direction. Promotion to a directional bias requires the next
confirmation. A CHOCH is a warning, not a reversal.

## 5. Market Structure Shift (MSS)

An MSS is a CHOCH that additionally satisfies:

```
displacement >= mss_min_displacement_atr × ATR      (default 0.75, > BOS threshold)
```

The distinction is deliberate. Every MSS is a CHOCH; not every CHOCH is an MSS. Setups
in this system require an MSS, not merely a CHOCH, because the displacement is the
evidence that the participants who defended the level have been removed.

## 6. Internal vs external structure

* **External**: swings passing the `swing_min_atr` filter on the analysed timeframe.
* **Internal**: swings that fail it, or swings detected with the smaller
  `internal_swing_lookback`.

Internal structure may confirm an entry. It may never establish, or contradict, the
higher-timeframe directional bias.

## 7. Strong vs weak highs and lows

* A swing high is **STRONG** if price failed to take it and instead broke structure
  downward afterwards. Strong highs are where sell-side interest defended.
* A swing high is **WEAK** if it was subsequently taken out.
* **UNTESTED** until one of the two occurs.

Strong highs and weak lows are the reliable draws on liquidity, and this classification
feeds directly into target selection.

## 8. Bias

The bias of a timeframe, evaluated in this order:

0. Fewer than `max(3 × atr_period, 50)` bars on the timeframe → `NEUTRAL`. A bias
   derived from 25 bars is noise wearing a label, and it would otherwise propagate into
   the HTF-alignment gate as though it were knowledge.
1. Fewer than 2 structural swings of either kind → `NEUTRAL` (unknown, not neutral-ish).
2. Most recent structural event is a bullish BOS → `BULLISH`.
3. Most recent is a bearish BOS → `BEARISH`.
4. Most recent is a CHOCH → `NEUTRAL` (see §4).
5. Otherwise, from the swing labels: HH+HL → `BULLISH`; LH+LL → `BEARISH`; mixed →
   `NEUTRAL`.

`NEUTRAL` is a real answer that blocks A+ classification. It is never coerced into a
direction to make a setup possible.

## 9. Dealing range

The range price is currently trading inside, used for premium/discount:

* **high** = most recent structural swing high not yet broken by a body close
* **low** = most recent structural swing low not yet broken by a body close
* **equilibrium** = midpoint

A long in premium and a short in discount are both penalised by the scoring engine and,
above a threshold, blocked outright.
