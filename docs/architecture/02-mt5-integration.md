# 02 — MT5 Integration Design

## 1. The constraints that drive this design

The official `MetaTrader5` Python package is not a normal API client, and pretending otherwise is
how MT5 bots lose money:

1. **Windows-only, terminal-required.** It IPCs into a running MT5 terminal. No terminal, no API.
2. **Process-global state.** `mt5.initialize()` binds the *process*, not an object. Two logins in
   one process is not a supported concept.
3. **Not thread-safe.** Concurrent calls from multiple threads produce corrupt results and
   occasional hard crashes of the terminal.
4. **Silent staleness.** `symbol_info_tick()` happily returns the last known tick forever after the
   feed dies. Freshness must be checked explicitly, every time.
5. **Broker-specific everything.** Symbol name, contract size, tick value, digits, stops level,
   filling mode and execution mode all vary per broker and per account type.
6. **`order_send` can be ambiguous.** A timeout does not tell you whether the order reached the
   server. Resending is how one intended position becomes two.

## 2. Topology: the bridge

```
┌───────────── engine process (any OS) ──────────────┐
│  Broker (Protocol)                                  │
│    ├── Mt5GrpcBroker   ──► gRPC ──►  mt5-bridge     │
│    ├── SimBroker       (backtest, in-process)       │
│    └── PaperBroker     (live data, simulated fills) │
└─────────────────────────────────────────────────────┘

┌───────────── mt5-bridge process (Windows only) ─────────────┐
│  gRPC server (grpc.server, max_workers=1)                    │
│         │                                                    │
│         ▼                                                    │
│  single-threaded request queue  ──►  MetaTrader5 binding     │
│         │                                                    │
│  ├── connection supervisor (initialize / login / retry)      │
│  ├── health monitor (terminal_info, account_info, tick age)  │
│  ├── tick streamer (server-side stream to engine)            │
│  └── calendar relay (reads terminal calendar via MQL5 EA)    │
└──────────────────────────────────────────────────────────────┘
```

`max_workers=1` plus an explicit serialisation queue means **every** MT5 call happens on one
thread in a defined order. All concurrency lives on the engine side of the wire.

The `Broker` Protocol is the whole reason the engine is portable and testable:

```python
class Broker(Protocol):
    def account(self) -> AccountState: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...
    def quote(self, symbol: str) -> Quote: ...
    def bars(self, symbol: str, tf: Timeframe, count: int, end: datetime | None) -> BarSeries: ...
    def positions(self, magic: int | None = None) -> list[Position]: ...
    def orders(self, magic: int | None = None) -> list[Order]: ...
    def send_market(self, req: MarketOrderRequest) -> OrderResult: ...
    def modify_position(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult: ...
    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult: ...
    def cancel_order(self, ticket: int) -> OrderResult: ...
    def health(self) -> BrokerHealth: ...
```

Three implementations, one interface. The backtester is not a parallel universe — it is
`SimBroker`.

## 3. Connection lifecycle

```
initialize(terminal_path)          → fail fast if terminal not found
login(account, password, server)   → if credentials supplied; else attach to logged-in terminal
verify:
    terminal_info().connected      == True
    terminal_info().trade_allowed  == True    (AutoTrading button — a real hardware kill switch)
    account_info().trade_allowed   == True
    account_info().trade_expert    == True
    account_info().margin_mode, currency, leverage  → recorded
resolve symbol (§4)
subscribe:  symbol_select(symbol, True)
warm caches: specs, session schedule, initial bars
publish READY
```

Supervision: a health ping every second. Three consecutive failures → `BROKER_UNREACHABLE` kill
switch, engine stops opening trades, reconnect with exponential backoff (2s → 60s cap). On
reconnect the engine **reconciles before it resumes** — never assumes its pre-disconnect view of
positions is still true.

## 4. Symbol discovery — never hardcoded

Broker gold symbols include `XAUUSD`, `XAUUSD.a`, `XAUUSDm`, `XAUUSD.raw`, `XAUUSD_i`, `XAUUSD.pro`,
`GOLD`, `GOLD.spot`, `XAUUSD-ECN`, and more. Resolution is automatic and verified:

```
1. If config.symbol_override is set → use it, still run validation below.
2. Enumerate mt5.symbols_get()  (all symbols, including hidden ones).
3. Candidate filter:
      name matches  ^(XAU|GOLD)  with optional broker suffix,   AND
      profit currency == "USD",                                  AND
      trade_mode == SYMBOL_TRADE_MODE_FULL,                      AND
      the symbol is not an index/basket (path/description sanity check)
4. Rank candidates by:
      + tradable now / has a live tick
      + tighter typical spread (sampled)
      + higher digits precision
      + shorter name (prefer the primary symbol over exotic variants)
5. Sanity-check the winner:
      quote is within a plausible gold range,
      point/digits consistent with contract size,
      symbol_info_tick() age is fresh
6. Persist the resolution + the full spec snapshot to `symbol_specs`.
7. If 0 candidates → refuse to start. If >1 plausible → refuse to start and ask the operator
   to set symbol_override. Ambiguity is never silently resolved.
```

The resolved symbol and its spec hash are re-verified every startup and every 24h; a change in
contract size or tick value mid-flight trips the kill switch, because it invalidates every open
position's risk calculation.

## 5. Symbol specification — read, never assumed

Everything below is read from `SymbolInfo` at runtime and stored:

| Field | Used for |
|---|---|
| `digits`, `point` | price rounding, all point↔price conversions |
| `trade_contract_size` | notional and value-per-point |
| `trade_tick_size`, `trade_tick_value` (+ `_profit` / `_loss` variants) | **the money value of the stop distance** |
| `volume_min`, `volume_max`, `volume_step` | lot rounding and clamping |
| `trade_stops_level` | minimum SL/TP distance from price |
| `trade_freeze_level` | distance inside which modify/close is refused |
| `spread`, `spread_float` | live cost and abnormality detection |
| `filling_mode` | choosing FOK / IOC / RETURN correctly per broker |
| `trade_mode` | is the symbol tradable at all right now |
| `sessions_quotes` / `sessions_trades` | broker's real trading hours, incl. the daily break |
| `swap_long`, `swap_short`, `swap_mode` | overnight cost in backtest and in hold decisions |
| `currency_profit`, `currency_margin` | FX conversion when the account is not USD |
| `margin_initial`, `margin_maintenance` | margin headroom checks |

**Commission is not in `SymbolInfo`.** It is inferred from executed deals
(`history_deals_get` → `DEAL_ENTRY_*` commission field) and, until there is fill history,
taken from an operator-supplied config value. Backtests use the configured value; the validation
report states which was used.

## 6. Position sizing — exact, defensive, verified

The whole method:

```
1. equity        = broker.account().equity                (re-read now, not cached)
2. risk_pct      = min(class_cap, daily_budget_left, weekly_budget_left,
                       monthly_budget_left, exposure_headroom, confidence_scale)
3. risk_money    = equity * risk_pct                       (in ACCOUNT currency)
4. sl_distance   = abs(entry - stop_loss)                  (in price units)
5. ticks         = sl_distance / spec.trade_tick_size
6. loss_per_lot  = ticks * spec.trade_tick_value_loss      (broker's own number)
7. if account currency != profit currency:
       loss_per_lot *= fx_rate(profit_ccy -> account_ccy)  (live, from MT5)
8. raw_lots      = risk_money / loss_per_lot
9. lots          = floor_to_step(raw_lots, spec.volume_step)     ← ALWAYS floor, never round
10. clamp:  lots = min(max(lots, 0), spec.volume_max)
11. if lots < spec.volume_min → REJECT the trade ("account too small for a structural stop")
                                 — never shrink the stop to fit the lot size
12. add commission + expected slippage into realised risk:
       realised_risk = lots * loss_per_lot + commission_est + slippage_est
13. ASSERT realised_risk <= equity * class_cap * (1 + tolerance)   → else raise + kill switch
14. margin check:  mt5.order_calc_margin(...) <= free_margin * margin_safety_factor
15. verify with the broker's own maths:
       mt5.order_calc_profit(ORDER_TYPE, symbol, lots, entry, stop_loss)
       must agree with our computed risk within tolerance, else REJECT
```

Step 15 is the important one and is rarely done: we compute the risk ourselves *and* ask MT5 to
compute the same loss, and refuse to trade if the two disagree. That single cross-check catches
every class of unit error — wrong tick value, wrong contract size, a currency conversion we
forgot, a broker with an unusual gold contract — before it reaches the market.

Step 9 floors rather than rounds, so rounding can only ever reduce risk. Step 11 refuses to trade
rather than compromise the stop: shrinking a structural stop to fit lot granularity is exactly the
kind of quiet compromise that destroys an edge.

## 7. Order execution

### Pre-send checklist (all re-derived at send time, nothing trusted from earlier)

```
[ ] bridge healthy, terminal connected, trade_allowed, trade_expert
[ ] kill switch clear
[ ] symbol resolved, spec hash unchanged, symbol tradable now (session open)
[ ] quote fresh (< max_quote_age) and inside a sane band
[ ] spread <= max_spread AND <= k * rolling_median_spread
[ ] no existing position or pending order with this strategy tag  (duplicate guard)
[ ] total open risk + this trade <= max_total_open_risk
[ ] daily / weekly / monthly drawdown budgets not breached
[ ] equity re-read; lots recomputed from the CURRENT price, not the signal price
[ ] entry slippage from signal price <= max_entry_drift, else abandon (not chase)
[ ] SL distance >= stops_level, TP distance >= stops_level, both normalised to `digits`
[ ] RR recomputed after normalisation still >= 2.0     ← re-checked post-rounding
[ ] margin sufficient
```

If the price has moved such that RR has fallen below 2.0 while the checklist ran, the trade is
abandoned. The setup is not chased.

### Idempotency — the duplicate-order problem

Every intended trade gets a **deterministic client tag** before any network call:

```
client_tag = blake2s(strategy | direction | decision_id | entry_bar_ts)[:12]
magic      = stable integer derived from strategy id
comment    = f"{strategy_short}:{client_tag}"
```

The tag is written to `orders` with status `INTENT` *before* `order_send`. Then:

- **Success** → record ticket, status `SENT`, then confirm by reading it back.
- **Explicit rejection** → classified and journalled; no retry unless the code is retryable.
- **Ambiguous** (timeout / disconnect / no result) → **never resend.** Enter `RECONCILING`:
  poll `positions_get`, `orders_get`, and `history_orders_get`/`history_deals_get` for the tag,
  for up to N seconds. Ground truth from the broker decides. If it cannot be established, trip the
  kill switch and alert a human. An unknown order state is a stop-everything condition.

On startup the same reconciliation runs across all `INTENT`/`SENT`/`RECONCILING` rows before the
engine is allowed to trade.

### Return-code taxonomy

| Class | Codes (examples) | Action |
|---|---|---|
| Success | `DONE`, `DONE_PARTIAL`, `PLACED` | confirm, persist; partial → decide top-up vs accept per config, never blind top-up |
| Retryable-with-reprice | `REQUOTE`, `PRICE_CHANGED`, `PRICE_OFF` | re-quote, re-validate RR, retry up to N; abandon if RR breaks |
| Retryable-transient | `TIMEOUT`, `CONNECTION`, `TOO_MANY_REQUESTS` | backoff; **reconcile before each retry** |
| Fix-and-retry-once | `INVALID_STOPS`, `INVALID_VOLUME`, `INVALID_FILL` | re-read spec, re-normalise, one retry; then abandon |
| Terminal | `NO_MONEY`, `TRADE_DISABLED`, `MARKET_CLOSED`, `LIMIT_POSITIONS` | abandon, alert, consider kill switch |
| Unknown | anything unmapped | abandon + kill switch + alert |

### Filling mode

Chosen from `symbol_info.filling_mode` bitmask rather than hardcoded, with a probe on first
connect: try the broker's declared preference, and on `INVALID_FILL` step through the supported
alternatives once and cache the winner per symbol per account. Hardcoding `ORDER_FILLING_IOC` is a
common cause of "the bot never fills at broker X".

## 8. Position management

Once open, a position is owned by the position manager, driven by the tick monitor and M1/M5 closes:

- **Stop loss lives on the broker, always.** Set in the same `order_send` where possible; if the
  broker rejects SL-on-entry (some ECN configs do), it is attached immediately after and, failing
  that, the position is closed. A position without a server-side stop is not permitted to persist.
- **Break-even** at a configured R multiple, respecting `stops_level` and `freeze_level`.
- **Partial take-profit** at TP1 (typically 2R) with the remainder running to a structural TP2,
  only where the broker permits partial closes and only when validated to improve expectancy —
  partials often *reduce* it, so this is a tested parameter, not an assumption.
- **Trailing** only by structure (behind newly formed swing points), never a fixed pip trail.
- **Time stop**: exit if the thesis has not begun to work within N bars — dead trades tie up risk
  budget.
- **Invalidation exit**: exit if the structural premise breaks (e.g. opposing MSS on the setup
  timeframe) even if the stop has not been hit.
- **Stops are NEVER widened.** The modify path asserts `new_risk <= current_risk` and raises
  otherwise. This is enforced in code, not convention.
- **Never average in.** Adding to a losing position is not an operation the `Broker` interface
  exposes at all — the capability simply does not exist in the system.

## 9. Reconciliation

Every 60 seconds, and on every startup, reconnect, and pre-send:

```
broker_positions = broker.positions(magic=OUR_MAGIC)
db_positions     = repo.open_positions()

diff → classify:
  in broker, not in DB   : adopt if tagged as ours (crash recovery) ; else ALERT (manual trade?)
  in DB, not in broker   : closed externally (SL/TP hit, manual close, stop-out) → close out record
  SL/TP mismatch         : broker is truth; restore intended stops or alert on conflict
  volume mismatch        : partial close happened → record it
  untagged position on our symbol : ALERT — a human is trading the same account
```

The broker is always the source of truth. The database is corrected to match, and any divergence
that cannot be explained trips the kill switch.

## 10. Testing without a terminal

- **Contract tests** run against the gRPC interface with a scripted fake bridge covering every
  return code, timeout, and partial fill.
- **Recorded-session replay**: bridge responses from demo sessions are recorded and replayed, so
  execution paths are tested against real broker behaviour on any OS.
- **Property tests** on sizing: for randomly generated valid `SymbolSpec` × price × SL × equity,
  computed risk must never exceed the cap, and lots must always be a valid multiple of
  `volume_step` within `[volume_min, volume_max]` or the trade must be rejected.
- A live smoke test on a demo account is a required gate before Stage 5 in the roadmap.
