# CLAUDE.md

Guidance for working in this repository.

## What this is

A selective XAUUSD trading system for MetaTrader 5. Not an indicator EA — an analysis
pipeline that journals an auditable decision for every evaluation and routes only
validated A/A+ setups to execution.

## Ground rules

**The system's value is the trustworthiness of its record, not its cleverness.** Before
adding a feature, ask whether it makes the journal more or less believable.

**Never weaken a risk invariant to make something work.** The 1:2 floor, the 1%/2% caps,
the drawdown lockouts and the stop-widening prohibition are the product. If a change
requires relaxing one, the change is wrong.

**`MarketView` is sacred.** Nothing may read data timestamped after the evaluation
instant. If you need forward data for labelling, it goes in `ml/labeling.py`, which is
outside the decision path and never feeds a feature.

**Degradation is one-directional.** Every missing input must make the system *less*
willing to trade. A dead news feed floors risk at MODERATE, not LOW. Stale macro is
UNKNOWN, not NEUTRAL. Nothing fails open.

## Layout

```
src/xauusd/
  domain/       frozen value objects and enums — invariants enforced at construction
  config/       layered YAML + env, validated at startup
  data/         MarketView (the look-ahead boundary), BarSeries, resampling, providers
  core/         structure, liquidity, FVG, order blocks, S/R, sessions, regime, analyzer
  intelligence/ DXY, macro, economic calendar, news
  strategy/     setups, features, scoring, gates, classifier
  risk/         sizing, drawdown, kill switch, and the RiskGate choke point
  execution/    broker protocol, MT5 bridge, order/position managers, reconciler
  backtesting/  engine, metrics, walk-forward, Monte Carlo, deployment gate
  ml/           triple-barrier labelling, purged CV, calibrated probability model
  engine/       decision pipeline and the live orchestrator
  dashboard/    FastAPI backend + no-build single-page terminal
```

## Commands

```bash
python -m xauusd.cli doctor          # pre-flight: config, DB, broker, symbol spec
python -m xauusd.cli backtest --synthetic 30000 --step 6
python -m xauusd.cli explain <id>    # full reasoning for one decision
python -m xauusd.cli rejections      # why the bot did not trade
python scripts/run_validation.py --synthetic 60000    # the Phase 10 deployment gate

pytest tests/unit -q                 # fast
pytest tests/integration -q          # includes the backtest/live parity gate
ruff check src tests && ruff format src tests
```

## Conventions

- Timestamps are `timestamptz` in UTC everywhere. Sessions derive from tz-aware local
  times via `zoneinfo`, never fixed offsets.
- R-multiples are the reporting unit. Currency P&L is a side effect of account size.
- Indicators return NaN during warm-up, never a fallback value — ATR scales every
  structural threshold, so a made-up ATR silently redefines a break of structure.
- Broker specs are always read, never assumed. There is no hardcoded contract size.
- New detection logic gets a spec in `docs/specs/` before it gets code.

## Testing

`risk/` and `execution/` carry a coverage floor because a bug there costs money. Every
"NEVER ALLOWED" behaviour from the brief has a test asserting it is *impossible*, not
merely absent.

The parity test in `tests/integration/test_parity.py` is the guard that keeps backtest
and live from drifting apart. If it fails, no validation number can be trusted until it
is fixed.

## Before changing analytics

Read `docs/FINDINGS.md` first. Several metrics have already been wrong in ways that
would have corrupted the deployment gate, and the notes explain what to watch for.
