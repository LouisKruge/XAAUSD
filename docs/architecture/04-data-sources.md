# 04 — Data Source Requirements

Every feed below is behind a provider interface with a defined fallback and an explicit
"what happens when it is unavailable" rule. **No feed is allowed to fail open.** If a required
input is missing or stale, the affected gate fails closed and the system does not trade.

---

## 1. XAUUSD price — live

| | |
|---|---|
| **Primary** | The MT5 broker's own feed, via the bridge |
| **Why** | Execution happens against this feed. Analysing a different gold feed and executing on the broker's introduces a basis you cannot see. Broker quotes differ by several tens of cents and by session boundaries. |
| **Timeframes** | M1, M5, M15, H1, H4, D1, W1, MN1 — pulled with `copy_rates_from_pos`, closed bars only |
| **Method** | Poll on bar close with a small guard delay; verify the returned bar's timestamp matches the expected boundary; back-fill gaps on reconnect |
| **Also captured** | Per-bar `spread`, tick stream (for spread stats, freshness, and intrabar backtest resolution) |
| **Failure** | Stale > threshold → `STALE_DATA` kill switch. Gap detected → back-fill, and mark affected analysis as degraded until continuity is restored. |

Bars are stored under `source = 'mt5:<broker>'` and never mixed with third-party history.

## 2. XAUUSD price — historical (backtesting)

Brokers typically retain limited M1 depth, so a research history is needed.

| Priority | Source | Notes |
|---|---|---|
| 1 | **Broker M1 via MT5** (`copy_rates_range`, terminal history depth raised) | Highest fidelity to what we will actually trade. Usually 1–5 years. Always harvested first. |
| 2 | **Dukascopy tick data** (free, `dukascopy-node`/`duka`) | Tick-level from ~2003, spread-inclusive bid/ask. Best free source for depth and for building a realistic spread model. |
| 3 | **HistData.com M1** (free) | Long history, but bid-only and no spread — usable for structure research, not for costing. |
| 4 | **Commercial tick data** (Tickstory / TickData / broker-provided) | Optional upgrade if free sources prove insufficient. |

**Requirements:** minimum 8 years to cover multiple regimes (2016–2018 range, 2019–2020 crisis
melt-up, 2021–2022 rate-hike bear pressure, 2023–2024 central-bank-buying bull, 2025+). Every
backtest records `data_source` and `data_hash`; a validation report that mixes sources is invalid.
Where the research source and the broker source overlap, a **basis report** quantifies the
difference, and validation results are discounted accordingly.

## 3. DXY / US Dollar

| Priority | Source | Notes |
|---|---|---|
| 1 | Broker's `USDX`/`DXY`/`USDIDX` CFD via MT5 | Same clock, same connection, intraday. Not all brokers offer it. |
| 2 | **Synthetic DXY computed from MT5 FX majors** | `50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 × USDSEK^0.042 × USDCHF^0.036`. Always available (every broker quotes these), same clock, backtestable over the same history. **This is the recommended default.** |
| 3 | FRED `DTWEXBGS` (broad dollar index) | Daily only; used as a slow confirmation series, not intraday. |

The engine consumes: level, 1d/5d/20d change, trend state, correlation with XAUUSD over rolling
windows, and — importantly — a **divergence flag** (gold and DXY rising together is a regime
signal in itself, not an error). The synthetic index is validated against a reference DXY at build
time and the tracking error recorded.

## 4. US Treasury yields & real yields

| Series | FRED id | Frequency | Use |
|---|---|---|---|
| 2y nominal | `DGS2` | daily | policy-expectation proxy |
| 10y nominal | `DGS10` | daily | headline rate level |
| **10y real (TIPS)** | `DFII10` | daily | **the single most important macro driver of gold** |
| 5y real | `DFII5` | daily | secondary |
| 10y breakeven inflation | `T10YIE` | daily | inflation expectations |
| Fed funds effective | `DFF` / `FEDFUNDS` | daily/monthly | policy stance |
| Yield curve 10y−2y | `T10Y2Y` | daily | recession/risk-off context |

- **Access:** FRED API — free, requires a key, generous limits. `fredapi` or direct HTTP.
- **Vintage:** FRED exposes ALFRED vintages; observations are stored with `release_ts`, and
  historical reads never see a value before it was published.
- **Intraday enhancement (optional):** the broker's 10Y note / `US10Y` CFD if offered, else CME
  ZN futures via a data vendor. Daily real yields are sufficient for bias; intraday nominal yields
  add responsiveness for the news layer.
- **Failure:** older than `max_macro_age` (default 3 business days) → fundamental alignment gate
  returns `UNKNOWN`, which blocks `A+` (which requires alignment) but not `A` (which requires
  only "not conflicting"). This is the intended graceful degradation.

## 5. Economic calendar

This is the hardest feed to get reliably and cheaply, so the design is deliberately layered.

| Priority | Source | Notes |
|---|---|---|
| 1 | **The MT5 terminal's own calendar, relayed by a small MQL5 EA** | MQL5 exposes `CalendarValueHistory` / `CalendarEventById` with actual/forecast/previous and impact. It is free, already installed, and on the broker's clock. The Python binding does not expose it, hence the EA writes to a file/socket the bridge reads. **Recommended primary.** |
| 2 | Commercial API — Trading Economics, Finnhub, FMP, or Econoday | Paid, clean, historical. Needed if the terminal calendar proves thin, and needed anyway for *historical* calendar data for backtesting. |
| 3 | **Manually curated YAML of critical events** | ~10 recurring events (FOMC decision + presser, NFP, CPI, PPI, PCE, ISM Mfg/Svc, GDP advance, JOLTS, Powell testimony) with their known publication schedule. This is the safety net: if every feed fails, blackout windows around the events that actually matter still apply. Small, auditable, and something a human maintains in minutes per quarter. |

**Our own impact mapping.** Provider "impact" ratings are inconsistent. We map to an internal
`normalized_key` → `(impact, gold_relevance 0..10)` table, so FOMC and CPI are treated as
CRITICAL for gold regardless of a provider's stars, and, say, a EUR-area consumer confidence print
is not.

**Blackout rules (all configurable per impact tier):**

```
CRITICAL (FOMC, NFP, CPI, PCE, Powell):
    pre-event blackout    : 60 min before   (no new entries)
    event blackout        : through release
    post-event stabilise  : 30 min minimum, AND spread back to <= 1.5 × median,
                            AND ATR(M5) back to <= 2 × pre-event, AND one M5 close
                            forming valid structure
HIGH   : 30 / 15 min
MEDIUM : 15 / 10 min
LOW    : no blackout, logged only
Open-position policy near CRITICAL events: configurable —
    default = tighten to break-even if in profit ≥ 1R, otherwise hold with the
    original server-side stop. Never widen. Never close blindly.
```

The post-event condition is a *state*, not a timer: "30 minutes" alone is how bots get filled into
a re-priced market.

## 6. News & geopolitical risk

| Layer | Source |
|---|---|
| Wire headlines | RSS/Atom: Reuters, AP, Bloomberg (where licensed), FT, CNBC, Kitco, Investing.com metals |
| Aggregated event data | **GDELT 2.0** (free, structured global event/tone data — good for a background geopolitical-tension index) |
| Structured news API | Marketaux / NewsAPI / Finnhub news (optional, paid tiers) |
| Central-bank primary sources | Fed, ECB, BoE, PBoC RSS + statement pages |
| Official gold demand | World Gold Council quarterly, IMF/central-bank reserve data (slow, contextual) |

**Processing pipeline:**

```
poll → dedupe by content hash → keyword/entity pre-filter (gold-relevant only)
     → rules-based classifier (cheap, deterministic, always runs)
     → LLM assessment (Claude, strict JSON schema, temperature 0, cached by content hash)
     → store in news_assessments, FROZEN at assessed_at
     → aggregate into news_risk_state (LOW | MODERATE | HIGH | EXTREME)
```

**Hard constraint: news never places a trade.** The news layer's only outputs are

1. a **risk level** that can veto or downgrade (HIGH blocks `A+`; EXTREME triggers blackout), and
2. a **small, bounded** contribution to fundamental alignment — capped at a few points of the
   100-point score, and unable on its own to move a candidate across a classification threshold.

The LLM output is schema-validated; anything unparseable is treated as `UNCERTAIN`, which raises
risk rather than lowering it. An unavailable news feed degrades to `MODERATE` risk (not `LOW`) —
absence of news is not evidence of calm.

## 7. Intermarket & positioning (secondary, phase 2+)

| Data | Source | Use |
|---|---|---|
| Silver, XAGUSD; gold/silver ratio | MT5 | precious-metals complex confirmation |
| SPX / VIX | MT5 CFDs or vendor | risk-on/risk-off regime |
| USDJPY, EURUSD | MT5 | dollar detail beyond the index |
| WTI / copper | MT5 | inflation-complex context |
| **CFTC COT gold positioning** | CFTC (free, weekly, Friday) | crowding / extreme-positioning flag |
| GLD ETF holdings | vendor / scraped | slow institutional flow |
| Central-bank gold buying | WGC quarterly | structural bid, contextual only |

These enter as *context features* for the model and small scoring adjustments; none is a trigger.

## 8. Reference & infrastructure data

| Data | Source |
|---|---|
| Broker symbol specs | MT5, snapshotted on change |
| Broker session schedule | MT5 `sessions_quotes` / `sessions_trades` |
| Broker server ↔ UTC offset | measured continuously from tick timestamps |
| Market holidays | derived from broker session data + a curated exchange-holiday list |
| DST rules | `zoneinfo` (IANA tzdata), kept updated |
| Account currency FX rate | MT5 (for non-USD accounts) |

## 9. Freshness policy

| Feed | Max age | On breach |
|---|---|---|
| Tick / quote | 5s (open market), session-aware | `STALE_DATA` kill switch |
| M5 bars | 1 bar + guard | pause cycle, back-fill, alert |
| Symbol spec | 24h | re-read; hash change → kill switch |
| DXY (synthetic) | same as ticks | DXY gate → `UNKNOWN` |
| Yields / macro | 3 business days | fundamental gate → `UNKNOWN` (blocks A+) |
| Calendar | 6 hours | fall back to YAML critical-events list; if that too is stale, treat all US sessions as HIGH risk |
| News | 30 minutes | risk level floors at `MODERATE` |

## 10. Costs

| Feed | Cost |
|---|---|
| MT5 price, specs, calendar-via-EA | free (broker account) |
| FRED | free (API key) |
| Dukascopy / HistData history | free |
| GDELT, CFTC, RSS, central-bank feeds | free |
| Claude API for news assessment | low — cached, filtered, batched; only gold-relevant items |
| Commercial calendar / news API | optional, ~$0–100/mo depending on tier |
| Windows VPS | ~$25–60/mo |

The system is designed to be fully functional on the free tier of every feed, with paid sources as
quality upgrades rather than dependencies.
