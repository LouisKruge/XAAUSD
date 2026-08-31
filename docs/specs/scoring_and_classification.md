# Spec — Scoring, Probability and Classification

## 1. The score is ordinal, not a probability

A confluence score out of 100 ranks candidates. It is **not** a probability, and the
system never treats it as one. The mapping from score-plus-features to
"probability this reaches +2R before −1R" is fitted separately, calibrated on
out-of-sample outcomes, and measured by Brier score and reliability curve.

## 2. Categories (total exactly 100)

| Category | Weight | What it measures |
|---|---|---|
| `htf_bias` | 15 | Agreement of MN/W/D/H4 with the trade direction. A conflict scores **zero**, not a small number. |
| `market_structure` | 15 | MSS presence and displacement; BOS worth much less; decays with age |
| `liquidity` | 15 | Sweep quality, displacement after, pool strength, recency, target liquidity |
| `fvg_ob` | 10 | Best zone quality, plus a bonus when FVG and OB overlap |
| `support_resistance` | 10 | Confluence at entry, weighted by importance |
| `fundamentals` | 10 | Macro alignment, plus a hard-capped news contribution |
| `dxy_yields` | 5 | Dollar and real-yield implications agreeing with the direction |
| `session` | 5 | London/NY/overlap, killzone bonus |
| `volatility_regime` | 5 | Regime alignment and a workable volatility band |
| `entry_confirmation` | 10 | Rejection strength, correct side, stop tightness, entry proximity |

> The example weights in the original brief total **95**, not 100. The missing five
> points are allocated to `entry_confirmation`, on the grounds that the execution
> trigger converts context into a fill and deserves parity with support/resistance.
> A validator enforces the 100 total so this class of error cannot ship silently.

## 3. Penalties (subtracted)

| Penalty | Max | Trigger |
|---|---|---|
| `news_risk` | 15 | Scaled: MODERATE 20%, HIGH 60%, EXTREME 100% |
| `fundamental_conflict` | 10 | Macro known and opposing, scaled by magnitude |
| `poor_volatility` | 8 | EXTREME full, LOW half |
| `wide_spread` | 8 | >2× median full, >1.5× half |
| `weak_session` | 5 | Asia or off-hours |
| `opposing_liquidity` | 10 | Resting opposite liquidity inside the stop distance |
| `stale_data` | 6 | News stale and/or macro unknown |
| `blocked_target` | 5 | A significant level between entry and target |

## 4. Breadth, not just total

A weighted sum can be inflated by one very strong signal. Classification therefore also
requires a minimum number of **independent categories** scoring at least
`strong_category_fraction` (default 0.7) of their maximum. Requiring breadth across
categories that are not derived from each other is a far better proxy for genuine
confluence than a high total.

## 5. Classification is a conjunction

`A` requires ALL of:

- every mandatory gate passed
- score ≥ `a_score_min`
- probability ≥ `a_probability_min` (when a healthy model exists)
- ≥ `a_strong_categories_min` strong categories
- RR ≥ 2.0
- no HTF conflict
- macro not opposing
- news risk ≤ MODERATE
- an MSS or a BOS present

`A+` requires all of the above at higher thresholds, PLUS:

- macro alignment **known and aligned** (not merely "not opposing")
- news risk exactly LOW
- an MSS specifically (a BOS is not enough)
- sweep quality > 0.5
- a calibrated probability — without a healthy model the system degrades to **A-only**

Anything else is `NO_TRADE`.

## 6. Risk is a ceiling, not an instruction

`A` caps at 1% and `A+` at 2% of equity. The sizing layer then takes the **minimum** of:

```
class cap
global cap                       (Stage 6 sets this far below the class caps)
remaining daily drawdown budget
remaining weekly drawdown budget
remaining monthly drawdown budget
exposure headroom
confidence-scaled risk
```

A+ never means "risk 2%". It means "2% is permitted if nothing else binds first", and
the journal records which constraint did bind.

## 7. Degradation, not failure

| Missing input | Behaviour |
|---|---|
| No probability model | A-only, score thresholds; recorded as `model_health=UNAVAILABLE` |
| Model unhealthy (drift) | A-only, then observation-only if drift worsens |
| Macro stale/unknown | `UNKNOWN` — blocks A+, allows A, small scoring credit |
| News feed down | Risk floors at MODERATE, never LOW; blocks A+ |
| Calendar feeds down | Curated fallback schedule; logged loudly |
| Dealing range unknown | No location score, no veto |

Nothing here fails **open**. Every degradation makes the system less willing to trade,
never more.
