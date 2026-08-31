# 01 — Technology Stack

Chosen for reliability and debuggability over novelty. Every dependency below is either
load-bearing or a clear net saving; anything that could be replaced by 40 lines of our own code
without loss has been left out deliberately.

## Runtime

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | `MetaTrader5`, the scientific stack, and `zoneinfo` in the stdlib. Not 3.13+ until the binding and LightGBM wheels are confirmed. |
| Package/env | **uv** + `pyproject.toml`, locked | Fast, reproducible, single lockfile; matters for a Windows VPS that must rebuild identically. |
| Concurrency | **asyncio** in the engine; the bridge is strictly synchronous single-threaded | Analysis is CPU-light and IO-bound. The bridge must be single-threaded because the MT5 binding is not thread-safe. |
| Config | **pydantic-settings v2** + layered YAML | Typed, validated at startup, fails loudly on a bad risk value instead of at 2 a.m. Config is hashed and the hash stored on every decision. |
| Process supervision | **NSSM** (Windows) / **systemd** (Linux) | Auto-restart with reconcile-on-boot. |

## Trading & broker

| Concern | Choice | Notes |
|---|---|---|
| Broker API | **`MetaTrader5`** official Python package | Windows-only, terminal-required. Quarantined in `mt5-bridge` (see `02`). |
| Bridge transport | **gRPC** (`grpcio` + protobuf) | Typed contract, streaming for ticks, first-class deadlines, trivially mocked in tests. Chosen over ZeroMQ (no schema) and REST (no streaming, worse latency). |
| Optional terminal-side helper | small **MQL5 EA** | Only for what the Python binding cannot do: pushing the terminal's built-in economic calendar, and a broker-side dead-man's switch. |

## Data & numerics

| Concern | Choice | Notes |
|---|---|---|
| Arrays | **NumPy 2.x** | Structure/liquidity engines operate on typed arrays, not DataFrames, in the hot path. |
| Frames | **Polars** (research + backtest) / small **pandas** surface at edges | Polars for the multi-year backtest scans; pandas only where a third-party lib demands it. |
| Columnar store | **Parquet** via **PyArrow**, queried with **DuckDB** | Historical research lake. DuckDB makes "win rate by session by regime by year" a single SQL query over Parquet with no server. |
| Indicators | **hand-written, vectorised NumPy** | ATR, RSI, MACD, VWAP, MAs are ~150 lines total. TA-Lib is a C build dependency on Windows and pandas-ta is unmaintained; neither is worth it. Ours are unit-tested against known vectors. |
| Stats | **SciPy**, **statsmodels** | Confidence intervals, stationarity, regression on macro series. |

## Machine learning

| Concern | Choice | Notes |
|---|---|---|
| Model | **LightGBM** | Tabular, small-sample, monotonic-constraint support (we constrain e.g. "more confluence never lowers probability"), fast to retrain. |
| Calibration | **scikit-learn** isotonic / Platt | The output that matters is a *calibrated* probability, measured by Brier score and reliability curve. |
| Validation | **`mlfinlab`-style purged & embargoed CV, implemented in-house** | Overlapping trade horizons make naive k-fold leak. We implement purging/embargo ourselves (~200 lines) rather than take a heavy dependency. |
| Experiment tracking | **MLflow** (local file backend) | Every model version, its features, its OOS metrics, its calibration curve — reproducible and diffable. |
| Serving | pickled model + feature-schema hash, loaded read-only by the engine | Engine refuses to load a model whose feature schema hash mismatches the code. |

The ML layer is deliberately *secondary*: the rule-based confluence engine is the primary
authority, and the model refines ranking and supplies calibrated probability. If the model is
unavailable or unhealthy, the system degrades to score-thresholds-only in `A`-only mode rather
than stopping — and says so on the dashboard.

## Storage

| Concern | Choice | Notes |
|---|---|---|
| Operational DB | **PostgreSQL 16** | Transactional integrity for orders/positions/risk state. JSONB for feature vectors. Advisory locks for the single-instance guard. |
| Time-series | **TimescaleDB** extension | Hypertables + compression for bars and ticks; continuous aggregates for higher timeframes derived from M1. |
| Migrations | **Alembic** | Schema is versioned from commit one. |
| Access | **SQLAlchemy 2.0 Core** (typed) + **asyncpg** | Core, not the full ORM, in hot paths; ORM-style models only for CRUD-ish tables. |
| Cache / bus | **Redis 7** | Pub/sub for dashboard streaming, hot state (current snapshot, kill-switch flag), distributed lock. |
| Research lake | Parquet on disk | Multi-year M1 + tick data. Cheap, portable, backupable. |

Postgres+Timescale is a real dependency to operate, and it is worth it: this system's whole value
is the integrity of its record. SQLite is supported for local unit tests only.

## Dashboard

| Concern | Choice | Notes |
|---|---|---|
| Backend | **FastAPI** + **uvicorn** | REST + WebSocket, Pydantic models shared with the engine, OpenAPI for free. |
| Frontend | **React 18** + **TypeScript** + **Vite** | Static build, no SSR needed for a single-operator terminal. |
| Styling | **Tailwind CSS** with a locked token set | Enforces the institutional palette: near-black `#0A0A0B` ground, white/grey type scale, colour reserved *only* for state (long/short/alert). No gradients, no rounded-2xl retail look. |
| Charts (price) | **TradingView lightweight-charts** | The only serious open-source candlestick renderer; overlays for FVG boxes, OB zones, liquidity lines, structure labels. |
| Charts (analytics) | **Recharts** | Equity curve, drawdown, distributions. |
| Data layer | **TanStack Query** + native WebSocket | Poll for history, stream for live. |
| Tables | **TanStack Table** | Dense, sortable decision/trade ledgers — the heart of the review workflow. |

## Observability

| Concern | Choice |
|---|---|
| Logging | **structlog** → JSON lines, rotating; correlation id per decision cycle |
| Metrics | **prometheus-client** exposed by engine + bridge (cycle latency, spread, equity, gate rejection counts, RPC errors) |
| Errors | **Sentry** (optional, self-hostable) |
| Alerts | Pluggable `Notifier`: **Telegram bot** (primary — instant, mobile), email fallback. Alerts are levelled; a kill-switch trip always alerts. |
| Health | `/health` on engine, bridge and api; a watchdog cron that alerts if the engine heartbeat row goes stale |

## Quality gates

| Concern | Choice |
|---|---|
| Tests | **pytest** + **pytest-asyncio**; **Hypothesis** for property tests on the structure/liquidity engines (e.g. "a BOS implies a prior swing", "sizing never exceeds the risk cap for any spec/price/SL triple") |
| Types | **mypy --strict** on `core/`, `risk/`, `execution/` |
| Lint/format | **ruff** |
| Coverage floor | 90% on `risk/` and `execution/`; these are the modules where a bug costs money |
| CI | **GitHub Actions**: lint, type, test, secret scan, and the **backtest/live parity replay test** |

## Deployment

- **Windows Server 2022 VPS** co-located near the broker (typically London/Equinix LD4 for most
  gold brokers) — required because MT5 is Windows-only and latency matters for stop management.
- Docker is used for Postgres/Redis; the engine runs natively on Windows via NSSM. (Running the
  MT5 terminal in a container is possible via Wine but is a reliability liability, not an asset.)
- Backups: nightly `pg_dump` + Parquet lake snapshot to off-box storage. The decision journal is
  the asset; losing it means losing the ability to validate anything.

---

## Implementation deviations from this document

Recorded here rather than left as a silent difference between plan and code. Each was
a deliberate call made while building, with the reasoning.

### 1. The MT5 bridge speaks length-prefixed JSON over TCP, not gRPC

gRPC's value here was a typed contract and streaming. The typed contract is preserved by
validating both ends against dataclasses; streaming is preserved by a subscribe frame.
What is dropped is a protoc build step on a Windows VPS whose whole appeal is being easy
to rebuild after a reinstall. The bridge carries a handful of calls per second over
localhost, where gRPC's throughput advantage is irrelevant and its operational cost is
not. The transport sits behind `BridgeTransport`, so swapping in gRPC later is one class.

*Where:* `src/xauusd/execution/bridge_protocol.py`

### 2. The dashboard is a no-build single-page app, not Vite + React + lightweight-charts

Same reasoning, plus one more: a trading box should not depend on a CDN it may not reach
when the network is degraded, and vendoring a chart library to avoid that costs more
than the ~200 lines of hand-rolled SVG actually needed (an equity curve with a drawdown
band, and horizontal bars). The API contract is unchanged, so a React front end can
replace one file without touching the backend.

*Where:* `src/xauusd/dashboard/static/`

### 3. The long/short palette is teal/coral, not green/red

Conventional trading green/red measures ΔE 4.1 under deuteranopia — effectively
indistinguishable for roughly one man in twelve. The pair actually used
(`#3fb6a8` / `#e8654f`) measures 11.8 and still reads as up/down. This was measured with
a palette validator rather than eyeballed. Colour is never the only cue regardless:
every direction carries a text label and every value carries a sign.

### 4. LightGBM thread count is pinned

LightGBM defaults to one thread per core, which under a cgroup CPU limit (a VPS, or CI)
causes severe thread thrashing — measured at **42.7 seconds** for a 600-sample fit
versus 0.02 seconds with `n_jobs=2`. Pinned deliberately, with the measurement recorded
in the code so nobody "optimises" it back.

*Where:* `src/xauusd/ml/model.py`

### 5. Redis is optional, not required

The architecture used Redis for the event bus, hot cache and distributed lock. As built,
the single-instance lock uses a Postgres advisory lock (or a PID file on SQLite), and the
dashboard hub is in-process. Redis remains the right answer when the dashboard and engine
run on separate hosts, and the `Hub` interface is unchanged for that case — but requiring
it for a single-operator single-box deployment was complexity without benefit.
