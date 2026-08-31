# 03 — Database Design

PostgreSQL 16 + TimescaleDB. Five logical groups:

1. **Market data** (hypertables, high volume)
2. **Analysis state** (what the engine saw, with lifecycle)
3. **Decisions & trading** (the audit spine)
4. **Risk & system state**
5. **Research: backtests, validation, models**

Conventions: all timestamps `timestamptz` stored UTC; money `numeric(20,8)`; prices `numeric(20,8)`
(never float — gold quotes at 2–3 decimals but position value must be exact); every mutable row
carries `created_at`/`updated_at`; nothing is ever hard-deleted from the audit spine.

---

## 1. Market data

```sql
CREATE TABLE bars (
    symbol           text        NOT NULL,
    timeframe        text        NOT NULL,          -- 'M1','M5','M15','H1','H4','D1','W1','MN1'
    ts               timestamptz NOT NULL,          -- bar OPEN time, UTC
    open             numeric(20,8) NOT NULL,
    high             numeric(20,8) NOT NULL,
    low              numeric(20,8) NOT NULL,
    close            numeric(20,8) NOT NULL,
    tick_volume      bigint      NOT NULL,
    real_volume      bigint,
    spread_points    integer,                       -- MT5 reports per-bar spread; vital for costing
    source           text        NOT NULL,          -- 'mt5:<broker>', 'dukascopy', ...
    is_final         boolean     NOT NULL DEFAULT true,
    PRIMARY KEY (symbol, timeframe, ts, source)
);
SELECT create_hypertable('bars','ts', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON bars (symbol, timeframe, ts DESC);

CREATE TABLE ticks (
    symbol   text NOT NULL,
    ts       timestamptz NOT NULL,
    bid      numeric(20,8) NOT NULL,
    ask      numeric(20,8) NOT NULL,
    last     numeric(20,8),
    volume   bigint,
    flags    integer,
    source   text NOT NULL
);
SELECT create_hypertable('ticks','ts', chunk_time_interval => INTERVAL '1 day');
-- retention: raw ticks 90 days; spread/quote statistics rolled up permanently
```

`bars` stores `source` in the key on purpose: the broker's feed and any third-party history are
kept separately and never silently merged, because they differ (different sessions, different
closes) and mixing them corrupts backtests.

```sql
CREATE TABLE symbol_specs (
    id                    bigserial PRIMARY KEY,
    broker                text NOT NULL,
    account_login         bigint NOT NULL,
    symbol                text NOT NULL,
    observed_at           timestamptz NOT NULL,
    spec_hash             text NOT NULL,            -- change detection
    digits                integer NOT NULL,
    point                 numeric(20,10) NOT NULL,
    contract_size         numeric(20,8) NOT NULL,
    tick_size             numeric(20,10) NOT NULL,
    tick_value            numeric(20,10) NOT NULL,
    tick_value_profit     numeric(20,10),
    tick_value_loss       numeric(20,10),
    volume_min            numeric(20,8) NOT NULL,
    volume_max            numeric(20,8) NOT NULL,
    volume_step           numeric(20,8) NOT NULL,
    stops_level           integer NOT NULL,
    freeze_level          integer NOT NULL,
    filling_modes         integer NOT NULL,
    trade_mode            integer NOT NULL,
    currency_profit       text NOT NULL,
    currency_margin       text NOT NULL,
    swap_long             numeric(20,8),
    swap_short            numeric(20,8),
    commission_per_lot    numeric(20,8),            -- inferred from deals or configured
    commission_source     text,                     -- 'inferred' | 'configured'
    raw                   jsonb NOT NULL,           -- full SymbolInfo dump
    UNIQUE (broker, account_login, symbol, spec_hash)
);

CREATE TABLE spread_stats (          -- rolling reference for "abnormal spread" detection
    symbol text NOT NULL, ts timestamptz NOT NULL,
    session text, median_points numeric, p95_points numeric, max_points numeric,
    sample_n integer, PRIMARY KEY (symbol, ts)
);
```

## 2. Exogenous data — vintage-aware

```sql
CREATE TABLE macro_series (
    series_id   text PRIMARY KEY,        -- 'DGS10','DFII10','T10YIE','DTWEXBGS','FEDFUNDS',...
    provider    text NOT NULL,
    name        text NOT NULL,
    units       text,
    frequency   text,
    gold_relevance text                  -- notes on interpretation direction
);

CREATE TABLE macro_observations (
    series_id   text NOT NULL REFERENCES macro_series(series_id),
    ref_date    date NOT NULL,           -- the period the value describes
    release_ts  timestamptz NOT NULL,    -- when it became PUBLICLY KNOWN  ← anti-leak key
    value       numeric(20,8),
    revision    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (series_id, ref_date, revision)
);
CREATE INDEX ON macro_observations (series_id, release_ts DESC);
```

> Every historical read filters `release_ts <= view.now`. Storing only `ref_date` — the usual
> shortcut — leaks revised data backwards and silently inflates every fundamental backtest.

```sql
CREATE TABLE calendar_events (
    id             bigserial PRIMARY KEY,
    external_id    text,
    source         text NOT NULL,             -- 'mt5_terminal','provider_x','manual'
    scheduled_ts   timestamptz NOT NULL,
    currency       text NOT NULL,
    country        text,
    name           text NOT NULL,
    normalized_key text,                      -- 'US_NFP','US_CPI_YOY','FOMC_RATE',...
    impact         text NOT NULL,             -- LOW | MEDIUM | HIGH | CRITICAL (our own mapping)
    gold_relevance smallint NOT NULL,         -- 0..10, our mapping, not the provider's
    actual         numeric(20,8),
    forecast       numeric(20,8),
    previous       numeric(20,8),
    revised_prev   numeric(20,8),
    actual_ts      timestamptz,               -- when the actual was published  ← anti-leak key
    first_seen_at  timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX ON calendar_events (scheduled_ts);

CREATE TABLE news_items (
    id           bigserial PRIMARY KEY,
    source       text NOT NULL,
    external_id  text,
    published_ts timestamptz NOT NULL,
    ingested_ts  timestamptz NOT NULL,
    headline     text NOT NULL,
    body         text,
    url          text,
    content_hash text NOT NULL UNIQUE
);

CREATE TABLE news_assessments (
    id               bigserial PRIMARY KEY,
    news_id          bigint NOT NULL REFERENCES news_items(id),
    assessed_at      timestamptz NOT NULL,       -- frozen; never regenerated for history
    assessor         text NOT NULL,              -- 'rules_v1' | 'llm:<model>@<promptver>'
    category         text NOT NULL,              -- WAR|CENTRAL_BANK|SANCTIONS|CRISIS|TRADE|OTHER
    importance       smallint NOT NULL,          -- 0..10
    gold_relevance   smallint NOT NULL,          -- 0..10
    direction        text NOT NULL,              -- BULLISH|BEARISH|NEUTRAL|UNCERTAIN
    uncertainty      smallint NOT NULL,          -- 0..10
    rationale        text,
    raw_response     jsonb,
    UNIQUE (news_id, assessor)
);

CREATE TABLE news_risk_state (        -- aggregated, time-series; what the engine actually consumes
    ts          timestamptz PRIMARY KEY,
    level       text NOT NULL,        -- LOW | MODERATE | HIGH | EXTREME
    drivers     jsonb NOT NULL,       -- contributing event/news ids + weights
    blackout    boolean NOT NULL,
    expires_at  timestamptz
);
```

## 3. Analysis state

```sql
CREATE TABLE market_snapshots (
    id             bigserial PRIMARY KEY,
    ts             timestamptz NOT NULL,       -- evaluation instant (M5 close)
    symbol         text NOT NULL,
    bias_mn        text, bias_w text, bias_d text,
    structure_h4   text, structure_h1 text, structure_m15 text, structure_m5 text,
    dealing_range  jsonb,                      -- {high, low, equilibrium, pd_zone}
    regime         text NOT NULL,              -- STRONG_BULL|MOD_BULL|RANGE|MOD_BEAR|STRONG_BEAR|...
    vol_regime     text NOT NULL,              -- LOW|NORMAL|HIGH|EXTREME
    atr_d1         numeric, atr_h1 numeric, atr_m15 numeric,
    session        text NOT NULL,              -- ASIA|LONDON|NY|OVERLAP|OFF
    killzone       text,
    spread_points  integer,
    dxy_state      jsonb,                      -- {level, chg_1d, chg_5d, trend, gold_implication}
    yields_state   jsonb,                      -- {us2y, us10y, real10y, breakeven, chg, implication}
    macro_bias     text,                       -- STRONGLY_BULLISH..STRONGLY_BEARISH (gold)
    news_risk      text NOT NULL,
    payload        jsonb NOT NULL,             -- full serialised analysis for replay
    config_hash    text NOT NULL,
    git_sha        text NOT NULL
);
SELECT create_hypertable('market_snapshots','ts', chunk_time_interval => INTERVAL '30 days');

-- Structural objects carry a lifecycle so "was it valid at time t?" is answerable.
CREATE TABLE structure_events (
    id bigserial PRIMARY KEY, symbol text NOT NULL, timeframe text NOT NULL,
    ts timestamptz NOT NULL,
    kind text NOT NULL,                         -- HH|HL|LH|LL|BOS|CHOCH|MSS
    direction text NOT NULL,                    -- BULL|BEAR
    price numeric(20,8) NOT NULL,
    ref_swing_ts timestamptz,
    displacement_atr numeric,                   -- displacement measured in ATR
    body_ratio numeric,
    strength text,                              -- STRONG|WEAK   (was the level defended?)
    is_internal boolean NOT NULL,               -- internal vs external structure
    meta jsonb
);
CREATE INDEX ON structure_events (symbol, timeframe, ts DESC);

CREATE TABLE liquidity_pools (
    id bigserial PRIMARY KEY, symbol text NOT NULL, timeframe text NOT NULL,
    kind text NOT NULL,             -- BSL|SSL|EQH|EQL|PDH|PDL|PWH|PWL|SESSION_HIGH|SESSION_LOW|RANGE_HIGH|RANGE_LOW
    price numeric(20,8) NOT NULL,
    price_upper numeric(20,8), price_lower numeric(20,8),
    formed_ts timestamptz NOT NULL,
    strength numeric,               -- touches, age, tf weight
    swept_ts timestamptz,           -- NULL = still resting liquidity
    sweep_quality numeric,          -- penetration depth / rejection strength / displacement after
    meta jsonb
);
CREATE INDEX ON liquidity_pools (symbol, swept_ts, price);

CREATE TABLE fvgs (
    id bigserial PRIMARY KEY, symbol text NOT NULL, timeframe text NOT NULL,
    direction text NOT NULL,        -- BULL|BEAR
    formed_ts timestamptz NOT NULL,
    top numeric(20,8) NOT NULL, bottom numeric(20,8) NOT NULL,
    size_points numeric NOT NULL, size_atr numeric NOT NULL,
    displacement_atr numeric,
    state text NOT NULL,            -- UNMITIGATED|PARTIAL|MITIGATED|INVERTED|INVALIDATED
    mitigated_pct numeric NOT NULL DEFAULT 0,
    first_touch_ts timestamptz, closed_ts timestamptz,
    quality_score numeric,          -- see scoring spec
    meta jsonb
);

CREATE TABLE order_blocks (
    id bigserial PRIMARY KEY, symbol text NOT NULL, timeframe text NOT NULL,
    kind text NOT NULL,             -- BULL_OB|BEAR_OB|BREAKER|MITIGATION
    formed_ts timestamptz NOT NULL,
    top numeric(20,8) NOT NULL, bottom numeric(20,8) NOT NULL,
    open_price numeric(20,8), close_price numeric(20,8),
    caused_bos_id bigint REFERENCES structure_events(id),
    swept_liquidity_id bigint REFERENCES liquidity_pools(id),
    has_fvg boolean NOT NULL DEFAULT false,
    displacement_atr numeric,
    state text NOT NULL,            -- FRESH|TESTED|MITIGATED|INVALIDATED
    test_count integer NOT NULL DEFAULT 0,
    quality_score numeric,
    meta jsonb
);

CREATE TABLE sr_levels (
    id bigserial PRIMARY KEY, symbol text NOT NULL, timeframe text NOT NULL,
    kind text NOT NULL,             -- SUPPORT|RESISTANCE|SUPPLY|DEMAND|RANGE_HIGH|RANGE_LOW
    price numeric(20,8) NOT NULL, band_upper numeric(20,8), band_lower numeric(20,8),
    formed_ts timestamptz NOT NULL, last_test_ts timestamptz,
    touch_count integer NOT NULL DEFAULT 0,
    rejection_strength numeric, importance numeric,
    state text NOT NULL, meta jsonb
);
```

## 4. Decisions & trading — the audit spine

```sql
CREATE TABLE decisions (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL,
    symbol            text NOT NULL,
    snapshot_id       bigint REFERENCES market_snapshots(id),
    strategy          text, strategy_version text,
    direction         text,                          -- LONG|SHORT|NULL when no candidate
    classification    text NOT NULL,                 -- NO_TRADE|A|A_PLUS
    setup_score       numeric,                       -- 0..100
    score_breakdown   jsonb,                         -- per-category earned/max + penalties
    probability       numeric,                       -- calibrated p(+2R before -1R)
    model_id          text, model_health text,
    features          jsonb NOT NULL,                -- the FULL feature vector, for replay/training
    gate_trace        jsonb NOT NULL,                -- ordered [{gate, verdict, observed, threshold}]
    blocking_gate     text,                          -- first failing gate, NULL if none
    all_blocking      text[],                        -- everything that would have blocked
    reasons_for       text[], reasons_against text[],
    planned_entry     numeric(20,8), planned_sl numeric(20,8),
    planned_tp1       numeric(20,8), planned_tp2 numeric(20,8),
    planned_rr        numeric, planned_risk_pct numeric, planned_lots numeric(20,8),
    invalidation      text,
    mode              text NOT NULL,                 -- BACKTEST|PAPER|DEMO|LIVE
    config_hash       text NOT NULL, git_sha text NOT NULL,
    latency_ms        integer
);
SELECT create_hypertable('decisions','ts', chunk_time_interval => INTERVAL '30 days');
CREATE INDEX ON decisions (classification, ts DESC);
CREATE INDEX ON decisions (blocking_gate, ts DESC);
CREATE INDEX ON decisions USING gin (features);
```

`decisions` is written on **every** evaluation cycle, including the thousands that end in
`NO_TRADE`. It is simultaneously the explainability record, the rejection ledger, and the training
set for the probability model. It is the single most valuable table in the system.

```sql
CREATE TABLE orders (
    id             bigserial PRIMARY KEY,
    decision_id    bigint REFERENCES decisions(id),
    client_tag     text NOT NULL UNIQUE,          -- deterministic idempotency key
    magic          bigint NOT NULL,
    symbol         text NOT NULL,
    side           text NOT NULL,                 -- BUY|SELL
    order_type     text NOT NULL,                 -- MARKET|LIMIT|STOP
    requested_volume numeric(20,8) NOT NULL,
    requested_price  numeric(20,8),
    sl numeric(20,8), tp numeric(20,8),
    status         text NOT NULL,                 -- INTENT|SENT|RECONCILING|FILLED|PARTIAL|REJECTED|CANCELLED|ABANDONED
    mt5_ticket     bigint,
    retcode        integer, retcode_text text,
    attempt        integer NOT NULL DEFAULT 1,
    sent_at timestamptz, confirmed_at timestamptz,
    raw_request jsonb, raw_result jsonb
);

CREATE TABLE fills (
    id bigserial PRIMARY KEY,
    order_id bigint REFERENCES orders(id),
    mt5_deal bigint UNIQUE, mt5_position bigint,
    ts timestamptz NOT NULL,
    volume numeric(20,8) NOT NULL, price numeric(20,8) NOT NULL,
    commission numeric(20,8), swap numeric(20,8), profit numeric(20,8),
    slippage_points numeric,                      -- requested vs filled → feeds backtest cost model
    entry_type text                               -- IN|OUT|INOUT|OUT_BY
);

CREATE TABLE positions (
    id bigserial PRIMARY KEY,
    mt5_position bigint UNIQUE,
    decision_id bigint REFERENCES decisions(id),
    strategy text, classification text,
    symbol text NOT NULL, side text NOT NULL,
    opened_at timestamptz NOT NULL, closed_at timestamptz,
    entry_price numeric(20,8) NOT NULL,
    initial_sl numeric(20,8) NOT NULL,            -- immutable — the risk denominator
    initial_tp numeric(20,8),
    current_sl numeric(20,8), current_tp numeric(20,8),
    volume numeric(20,8) NOT NULL, remaining_volume numeric(20,8) NOT NULL,
    risk_money numeric(20,8) NOT NULL, risk_pct numeric NOT NULL,
    exit_price numeric(20,8), exit_reason text,   -- SL|TP1|TP2|TRAIL|TIME_STOP|INVALIDATION|MANUAL|KILL_SWITCH
    gross_pnl numeric(20,8), commission numeric(20,8), swap numeric(20,8), net_pnl numeric(20,8),
    r_multiple numeric,                           -- net_pnl / risk_money  ← the unit of account
    mae_r numeric, mfe_r numeric,                 -- max adverse / favourable excursion in R
    bars_held integer, session text, regime text
);

CREATE TABLE position_events (                    -- every modification, fully audited
    id bigserial PRIMARY KEY,
    position_id bigint REFERENCES positions(id),
    ts timestamptz NOT NULL,
    kind text NOT NULL,                           -- BE_MOVE|TRAIL|PARTIAL_CLOSE|TP_ADJUST|CLOSE|ADOPT|ALERT
    old_sl numeric(20,8), new_sl numeric(20,8),
    old_tp numeric(20,8), new_tp numeric(20,8),
    volume_delta numeric(20,8), reason text, raw jsonb
);
```

`r_multiple` is the reporting unit throughout. Currency P&L is a side effect of account size;
R-multiples are what let a 2019 trade and a 2026 trade be compared.

## 5. Risk & system state

```sql
CREATE TABLE account_snapshots (
    ts timestamptz PRIMARY KEY,
    balance numeric(20,8), equity numeric(20,8), margin numeric(20,8),
    free_margin numeric(20,8), margin_level numeric,
    open_positions integer, open_risk_pct numeric, currency text, mode text
);
SELECT create_hypertable('account_snapshots','ts', chunk_time_interval => INTERVAL '30 days');

CREATE TABLE risk_state (
    period_type text NOT NULL,                 -- DAY|WEEK|MONTH
    period_start timestamptz NOT NULL,
    starting_equity numeric(20,8) NOT NULL,
    peak_equity numeric(20,8) NOT NULL,
    current_equity numeric(20,8) NOT NULL,
    realised_pnl numeric(20,8) NOT NULL DEFAULT 0,
    drawdown_pct numeric NOT NULL DEFAULT 0,
    limit_pct numeric NOT NULL,
    trades_taken integer NOT NULL DEFAULT 0,
    risk_deployed_pct numeric NOT NULL DEFAULT 0,
    locked_out boolean NOT NULL DEFAULT false,
    locked_out_at timestamptz, lockout_reason text,
    PRIMARY KEY (period_type, period_start)
);

CREATE TABLE kill_switch_events (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL,
    action text NOT NULL,                      -- TRIP|CLEAR
    reason_code text NOT NULL,                 -- DAILY_DD|WEEKLY_DD|MONTHLY_DD|BROKER_UNREACHABLE|
                                               -- STALE_DATA|SPREAD_ABNORMAL|SLIPPAGE|NEWS_EXTREME|
                                               -- STATE_DIVERGENCE|DUPLICATE|SPEC_CHANGED|MANUAL|SYSTEM_ERROR
    detail text, context jsonb,
    cleared_by text, auto_clearable boolean NOT NULL
);

CREATE TABLE system_health (
    ts timestamptz NOT NULL, component text NOT NULL,   -- engine|bridge|api|worker|db
    status text NOT NULL, latency_ms integer, detail jsonb,
    PRIMARY KEY (ts, component)
);

CREATE TABLE alerts (
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL,
    level text NOT NULL, category text NOT NULL,
    title text NOT NULL, body text, context jsonb,
    delivered boolean NOT NULL DEFAULT false, channel text
);

CREATE TABLE config_versions (
    config_hash text PRIMARY KEY, created_at timestamptz NOT NULL,
    git_sha text NOT NULL, content jsonb NOT NULL, note text
);
```

## 6. Research: backtests, validation, models

```sql
CREATE TABLE backtest_runs (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL,
    strategy text NOT NULL, strategy_version text NOT NULL,
    kind text NOT NULL,                    -- IN_SAMPLE|OUT_OF_SAMPLE|WALK_FORWARD|MONTE_CARLO|STRESS|SENSITIVITY
    period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
    data_source text NOT NULL, data_hash text NOT NULL,
    config jsonb NOT NULL, config_hash text NOT NULL, git_sha text NOT NULL,
    cost_model jsonb NOT NULL,             -- spread source, commission, slippage, latency
    metrics jsonb NOT NULL,                -- the full metric block below
    notes text
);

CREATE TABLE backtest_trades (             -- same shape as `positions` so analytics code is shared
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES backtest_runs(id),
    decision_snapshot jsonb NOT NULL,
    opened_at timestamptz, closed_at timestamptz,
    side text, classification text,
    entry numeric(20,8), sl numeric(20,8), tp numeric(20,8), exit_price numeric(20,8),
    exit_reason text, r_multiple numeric, mae_r numeric, mfe_r numeric,
    session text, regime text, score numeric, probability numeric
);

CREATE TABLE validation_reports (
    id bigserial PRIMARY KEY,
    strategy text NOT NULL, strategy_version text NOT NULL,
    created_at timestamptz NOT NULL,
    in_sample_run bigint REFERENCES backtest_runs(id),
    oos_run bigint REFERENCES backtest_runs(id),
    walk_forward_runs bigint[],
    monte_carlo jsonb, sensitivity jsonb, stress jsonb,
    verdict text NOT NULL,                 -- PASSED|FAILED|CONDITIONAL
    gate_results jsonb NOT NULL,           -- every criterion + observed value + pass/fail
    approved_regimes text[], approved_sessions text[],
    approved_by text, approved_at timestamptz,
    UNIQUE (strategy, strategy_version, created_at)
);

CREATE TABLE strategy_status (             -- the live-routing gate, enforced in code
    strategy text NOT NULL, strategy_version text NOT NULL,
    status text NOT NULL,                  -- DEV|IN_SAMPLE_PASSED|OOS_PASSED|PAPER|DEMO|LIVE|RETIRED
    validation_report_id bigint REFERENCES validation_reports(id),
    max_class text NOT NULL DEFAULT 'A',   -- may this strategy ever produce A+?
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (strategy, strategy_version)
);

CREATE TABLE models (
    model_id text PRIMARY KEY,
    created_at timestamptz NOT NULL,
    kind text NOT NULL,                    -- lightgbm|logistic|isotonic_calibrator
    feature_schema_hash text NOT NULL,     -- engine refuses to load on mismatch
    train_period tstzrange, oos_period tstzrange,
    metrics jsonb NOT NULL,                -- auc, brier, log_loss, calibration slope/intercept
    calibration jsonb NOT NULL,            -- reliability curve points
    mlflow_run_id text, artifact_path text NOT NULL,
    status text NOT NULL                   -- CANDIDATE|ACTIVE|RETIRED
);

CREATE TABLE model_health (                -- live drift monitoring
    ts timestamptz NOT NULL, model_id text NOT NULL REFERENCES models(model_id),
    window_trades integer, realised_win_rate numeric, predicted_win_rate numeric,
    brier numeric, psi numeric, verdict text,     -- HEALTHY|DRIFTING|DEGRADED
    PRIMARY KEY (ts, model_id)
);
```

### The metric block stored in `backtest_runs.metrics`

```
trades, wins, losses, breakevens,
win_rate, win_rate_wilson_lower_95,
profit_factor, expectancy_r, expectancy_money,
avg_win_r, avg_loss_r, avg_rr_realised, avg_rr_planned,
max_drawdown_pct, max_drawdown_r, max_drawdown_duration_days,
sharpe, sortino, calmar, recovery_factor,
max_consecutive_losses, max_consecutive_wins,
risk_of_ruin, ulcer_index,
exposure_pct_of_time, trades_per_month,
by_session{}, by_regime{}, by_day_of_week{}, by_hour{}, by_class{A, A+}, by_year{}
```

`win_rate_wilson_lower_95` matters more than `win_rate`: 7 wins in 10 trades is not evidence of a
70% strategy. The validation gate in `05` is written against the lower bound.

## 7. Derived views

```sql
CREATE MATERIALIZED VIEW mv_daily_performance AS ...;   -- equity, R sum, trade count per day
CREATE MATERIALIZED VIEW mv_rejection_ledger AS         -- "why no trades today?"
  SELECT date_trunc('day', ts) d, blocking_gate, count(*) FROM decisions
  WHERE classification = 'NO_TRADE' GROUP BY 1,2;
CREATE MATERIALIZED VIEW mv_class_performance AS ...;   -- A vs A+ realised expectancy
```

## 8. Retention & backup

| Data | Retention |
|---|---|
| Ticks | 90 days raw, then compressed spread/quote stats forever |
| M1 bars | forever (Timescale compression after 30 days) |
| Higher TF bars | forever, uncompressed (small) |
| `decisions` | forever — this is the research asset |
| `market_snapshots.payload` | full for 180 days, then pruned to summary columns |
| Orders / fills / positions | forever, immutable |

Nightly `pg_dump` + weekly full snapshot, off-box. An integrity job verifies every `positions` row
reconciles to its `fills` and every `orders` row reaches a terminal status.
