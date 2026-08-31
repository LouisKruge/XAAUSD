# XAUUSD Trading Intelligence & Execution System

A highly selective, multi-layered XAUUSD (Gold) trading system that integrates directly with
MetaTrader 5. It is **not** an indicator EA. It is an analysis pipeline that produces an
auditable decision — `NO_TRADE`, `A`, or `A+` — for every evaluation, and only routes `A` /
`A+` decisions to an execution layer guarded by hard risk invariants.

## Operating principle

> **NO TRADE IS BETTER THAN A LOW-QUALITY TRADE.**

The expected steady state of this system is *mostly idle*. If it trades often, something is wrong.

## Status

| | |
|---|---|
| **Phase** | 0 — Architecture (awaiting approval) |
| **Code written** | None yet, by design |
| **`LIVE_TRADING`** | `false` (default, and requires two-key arming to change) |

Implementation begins only after the architecture in `docs/architecture/` is approved.

## Documents

| Doc | Contents |
|---|---|
| [`00-overview.md`](docs/architecture/00-overview.md) | End-to-end system architecture, process topology, decision pipeline, timing model, backtest/live parity |
| [`01-tech-stack.md`](docs/architecture/01-tech-stack.md) | Exact technologies, libraries, versions and the reasoning |
| [`02-mt5-integration.md`](docs/architecture/02-mt5-integration.md) | How Python talks to and executes through MT5; symbol discovery, sizing, order safety |
| [`03-database.md`](docs/architecture/03-database.md) | Full schema design with DDL |
| [`04-data-sources.md`](docs/architecture/04-data-sources.md) | Every data feed: price, DXY, yields, calendar, news, history |
| [`05-roadmap.md`](docs/architecture/05-roadmap.md) | Phased build plan with acceptance gates |
| [`06-open-decisions.md`](docs/architecture/06-open-decisions.md) | Decisions needed from the account owner before Phase 1 |

## Non-negotiables encoded in this design

1. Risk per trade is capped at **1% (A)** / **2% (A+)** of current equity, computed from the
   broker's real symbol specification — never assumed values.
2. No martingale, no grid, no averaging into losers, no widening stops, no risk increase after losses.
3. Minimum **1:2** reward-to-risk; the trade is rejected otherwise. 1:3 only when real structure
   supports it.
4. Daily / weekly / monthly drawdown limits trigger a hard lockout.
5. A strategy may not be deployed live until it clears the validation gate in
   [`05-roadmap.md`](docs/architecture/05-roadmap.md) — and the gate is a *filter*, not a promise
   of live performance.
6. Every decision — taken and rejected — is journalled with the full feature vector, so
   "why did you (not) take that trade?" is always answerable from the database.
