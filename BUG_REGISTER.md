# BUG REGISTER

Opened 2026-09-06 under the Master Engineering Directive. Every entry below comes from
reading the code or running it, never from inference about what code of this shape
usually does. Where I could not verify something in this environment it says so, in the
entry, rather than being quietly counted as passing.

## Environment limits on verification — read this first

This audit runs in a **Linux container with no MetaTrader 5, no broker connection, and
no harvested price history**. MT5 is Windows-only and the bridge talks to a terminal on
the operator's machine. So:

| Directive item | Status here |
|---|---|
| §8 MT5 connection manager | Code audited; **live connection NOT VERIFIABLE HERE** |
| §9 order verification against a real terminal | Logic audited + simulated; **real MT5 NOT VERIFIABLE HERE** |
| §12 R300 viability against a real broker spec | Arithmetic tested; **real spec NOT VERIFIABLE HERE** |
| §26 live gate | Code audited; **cannot be exercised without a broker** |
| §31 dashboard against live data | Data paths audited; **live values NOT VERIFIABLE HERE** |
| §39 profitability on real history | **NOT POSSIBLE HERE** — the database is on the operator's machine |

Everything else is run, not assumed.

## Baseline before any change

```
unit         575 passed
integration  105 passed
ruff         clean      ruff format clean      mypy clean (risk, execution, domain)
```

A green suite is the starting point, not evidence of correctness: every bug below was
present while all 680 tests passed. That is the point of the audit.

---

## CRITICAL — prevents trading or creates unsafe trading

### BUG-001 — A broker pricing failure silently disables the sizing cross-check

| | |
|---|---|
| **File** | `src/xauusd/engine/pipeline.py:172-178`, consumed at `src/xauusd/risk/position_sizing.py:158` |
| **Function** | `DecisionPipeline._broker_loss_for_one_lot` → `PositionSizer.size` |
| **Severity** | CRITICAL |
| **Status** | FIXED |

**Description.** The sizer cross-checks our loss-per-lot against the broker's own
`OrderCalcProfit`. If they disagree by more than the tolerance it refuses the trade —
"refusing to trade on a specification we cannot verify". That is the guard against
sizing on a misread contract spec, which is the single most expensive arithmetic error
this system can make.

The value reaching it comes from:

```python
try:
    value = state.calc_profit(plan.direction, plan.entry, plan.stop_loss)
    return float(value) if value is not None else None
except Exception:
    return None
```

and the consumer is `if broker_calc_profit is not None:`.

**Root cause.** `None` is overloaded to mean two incompatible things: *"there is no
broker to ask"* (correct in BACKTEST, where `SimBroker` has no `OrderCalcProfit`) and
*"the broker was asked and failed"* (a live fault). The second silently skips the check
and proceeds to trade on unverified arithmetic. Nothing is logged, so the trade's
journal cannot show the check was skipped.

**Dependencies.** `EngineState.calc_profit`, `RiskGate.evaluate`, `SizingResult`,
`ScalpPipeline` (passes `broker_calc_profit=None` unconditionally — see BUG-002).

**Fix.** Distinguish "not available" from "failed". Log the failure. In a real-money
mode, a broker that cannot price a test tick is a reason to refuse, not to skip —
degradation is one-directional everywhere else in this system and must be here.

**Test required.** A live-mode sizing call whose `calc_profit` raises must not approve.
A backtest-mode call with no `calc_profit` must still approve.

---

### BUG-002 — The scalp path never performs the broker sizing cross-check at all

| | |
|---|---|
| **File** | `src/xauusd/engine/orchestrator.py` (scalp scan), `src/xauusd/engine/scalp_pipeline.py` |
| **Function** | `TradingEngine._scalp_scan` → `ScalpPipeline.run(broker_calc_profit=None)` |
| **Severity** | CRITICAL |
| **Status** | FIXED |

**Description.** The live scalp scan passes `broker_calc_profit=None` as a literal. The
A/A+ path computes it from the broker. So the cross-check that refuses to size on an
unverifiable specification protects A/A+ trades and not scalp trades — on the same
account, through the same `RiskGate`, to the same broker.

**Root cause.** Same class as FINDINGS 38 and 40: a rule with several enforcement
points, and the newest path never learned it. This is the eleventh instance.

**Fix.** Wire the broker's `calc_profit` into the scalp scan exactly as the A/A+ path
does.

**Test required.** A parity test asserting both paths receive a broker cross-check value
when one is available.

---

## HIGH — breaks major functionality

### BUG-003 — A dead engine loop is invisible and the process still exits successfully

| | |
|---|---|
| **File** | `src/xauusd/engine/orchestrator.py:320-329` |
| **Function** | `TradingEngine.run` |
| **Severity** | HIGH |
| **Status** | FIXED |

**Description.**

```python
await asyncio.gather(
    self._tick_loop(), self._decision_loop(), self.scalp_scanner.run(),
    self._reconcile_loop(), self._context_loop(), self._command_loop(),
    return_exceptions=True,
)
```

The return value is discarded. `return_exceptions=True` collects each task's exception
into that list instead of propagating it, so if a loop dies nothing raises, nothing is
logged, and the remaining loops carry on. The engine keeps ticking and managing
positions with, say, no decision loop — alive, and not trading. When the loops finally
end, `run()` returns normally and `cmd_run` exits **0**, reporting success.

**Mitigating fact, established by reading each loop:** every loop has an internal
`except Exception` that logs and continues, so ordinary faults do not kill a loop. This
is why it is HIGH and not CRITICAL. What escapes that guard is anything outside the
`try` (`_reconcile_loop` and `_command_loop` both `await asyncio.sleep(interval)` before
theirs) and any `BaseException` — `CancelledError`, `MemoryError`.

**Fix.** Inspect the gathered results. Log any exception at CRITICAL with the task name,
stop the engine rather than continue degraded, and exit non-zero. §37 requires exactly
this.

**Test required.** A loop that raises must produce a logged CRITICAL, stop the engine,
and yield a non-zero exit.

---

### BUG-004 — A database failure silently disables live scalp routing with no explanation

| | |
|---|---|
| **File** | `src/xauusd/engine/orchestrator.py:747-755` |
| **Function** | `TradingEngine._strategy_status` |
| **Severity** | HIGH |
| **Status** | FIXED |

**Description.** `except Exception: return {}`. An empty map means every model reads
`DEV`, so `scalp_strategy_validated` refuses live routing. The **behaviour** is correct
and safe. The **observability** is not: an operator sees a bot that has stopped taking
scalps and no log line saying the strategy-status table could not be read. The safe
outcome is indistinguishable from a market with no setups — the confusion this project
has already paid for twice (FINDINGS 37, 41).

**Fix.** Log the exception at ERROR and surface it in the health panel. Keep the
fail-closed return.

**Test required.** A failing session must log and still return an empty map.

---

## MEDIUM — incorrect behaviour, system continues

### BUG-005 — `_worker_init` inherits a `SystemExit` path that breaks the pool

| | |
|---|---|
| **File** | `scripts/scalp_sweep.py` → `src/xauusd/cli.py:539` |
| **Severity** | MEDIUM |
| **Status** | FIXED |

`_load_data` raises `SystemExit` when history is missing. In a pool worker that kills the
process and surfaces as `BrokenProcessPool`. A serial fallback now catches it (commit
`42d464a`), so a run completes, but the diagnosis reaching the operator is the pool
error rather than "no history". Worth converting to a typed exception the worker can
report cleanly.

---

## Verified NOT defective (checked, found correct)

Recording these so the audit is not mistaken for a list of everything that is wrong.

| Area | Evidence |
|---|---|
| §4 bare `except:` | `grep -c "except\s*:" src/` → **0** |
| §5 mock data in live paths | Only 3 files match `mock/dummy/fake/placeholder`: `bootstrap.py`, `retcodes.py`, `symbol_discovery.py` — all string literals in messages or broker retcode names, none returning fabricated market data |
| §28 retry safety | `OrderManager` classifies retcodes and routes ambiguous sends to `_reconcile` before retrying, which is the check-before-retry the directive demands |
| §35 restart recovery | `reconciler.py` adopts orphaned broker positions on startup (`adopt_orphans`, `result.adopted`) |
| §27 kill switch | Typed reasons, non-auto-clearable ones need `force=True` and a named human, tripping is idempotent and alerts |
| News date parsing | `news_feed.py:44` `except: pass` is a legitimate format-fallback chain, not a swallow |

---

## Execution order

CRITICAL → HIGH → MEDIUM → LOW, fixing root causes, re-running the full suite after
each, and adding the test that would have caught it.

1. BUG-002 (scalp cross-check absent) — smallest fix, largest safety gap
2. BUG-001 (failure vs unavailable) — the semantic root of BUG-002's class
3. BUG-003 (dead loop invisible)
4. BUG-004 (silent DB failure)
5. BUG-005 (typed exception for missing history)


---

## Round 1 outcome

| Bug | Severity | Status | Test |
|---|---|---|---|
| BUG-001 broker failure vs unavailable | CRITICAL | **FIXED** | `test_broker_cross_check.py` (5), `test_trade_path.py` (+1 live case) |
| BUG-002 scalp path had no cross-check | CRITICAL | **FIXED** | `test_broker_cross_check.py` (5) |
| BUG-003 dead loop invisible, exit 0 | HIGH | **FIXED** | `test_engine_failure_visibility.py` (6) |
| BUG-004 silent DB failure | HIGH | **FIXED** | `test_engine_failure_visibility.py` (2) |
| BUG-005 SystemExit through a pool worker | MEDIUM | **FIXED** | `test_failure_modes.py` |

**A regression I caused and repaired.** Making `_broker_loss_for_one_lot` an instance
method — it needs `settings.mode` to tell a live failure from an absent broker — broke
two existing integration tests that called it statically. They are updated rather than
deleted, and the class gained the case whose absence let BUG-001 survive: a broker that
fails *on real money* must refuse, not skip. The old tests asserted only the lenient
half and were right about it; they were simply incomplete.

**Proof the new tests catch the old bug**, rather than merely passing against the fix:

```
BEFORE fix (HEAD)        : calc_profit present=False -> TEST FAILS (bug present)
AFTER fix (working tree) : calc_profit present=True  -> TEST PASSES
```

## Round 2

**BUG-005 FIXED.** `_load_data` raised `SystemExit`, which inherits from `BaseException`
and so passes straight through every `except Exception`. Inside a pool worker it killed
the process and the parent saw only `BrokenProcessPool` — an opaque message for a
condition whose fix is one sentence. Now `InsufficientHistory(RuntimeError)`; the CLI
catches it and exits 2 as before, and the sweep prints the real cause.

**§41 final sweep — clean.** `TODO/FIXME/HACK/XXX` in `src/`: **0**. Six bare `pass`
statements, each judged individually: five are empty exception-class bodies, one is
`except KeyboardInterrupt: pass` in the bridge's shutdown path followed by a `finally`
that stops the worker and the server. `print(` appears only in `cli.py` and
`config/bootstrap.py`, both user-facing entry points. Nothing deleted blindly.

**§34 failure injection — 10 tests added** (`tests/integration/test_failure_modes.py`),
all passing:

| Injected failure | Asserted response |
|---|---|
| Broker cannot price a lot, real money | Refuses (`BrokerCrossCheckUnavailable`) |
| Broker's loss/lot disagrees 10x | Refuses to size; `lots == 0` |
| Free margin far below requirement | Refuses to size; `lots == 0` |
| ATR still NaN (warm-up) | `MicroSnapshot.usable` False |
| Snapshot degraded despite good ATR | `usable` False |
| Unusable data reaching the scalp cycle | Cycle skips; **no model runs at all** |
| Kill switch tripped | Entries blocked with a stated reason |
| Non-auto-clearable reason | Will not clear itself; needs a named human + force |
| Broker holds a position we do not know | Adopted, not duplicated |
| We hold a record the broker does not | Not adopted; divergence recorded |

A first version of the recovery test used a hand-rolled position double and passed while
the reconciler would have broken on a field the double omitted. It now uses the real
`BrokerPosition`.

## Round 3

### BUG-006 — The session gate reported a self-contradiction

| | |
|---|---|
| **File** | `src/xauusd/strategy/gates.py` — `g_session` |
| **Severity** | HIGH (observability; it wasted operator time directly) |
| **Status** | **FIXED** |

Found in a live dashboard screenshot. The entire explanation the operator was given:

```
X  session   observed "LONDON" · required "['ASIA','LONDON','NEW_YORK','OVERLAP','OFF']"
```

LONDON is in that list. `is_tradable_window` refuses for **five** distinct reasons —
market closed, weekday not allowed, session not allowed, too soon after the weekly
open, too close to the weekly close — and the gate reported only the session name
against the allowed set, so four of the five rendered as nonsense. Reproduced against
the exact timestamp on the dashboard, 2026-09-06 06:30Z:

```
weekday=Sunday  tradable=False  reason='market closed'
```

The truth was in `detail` the whole time and the trace did not show it. A reader is sent
to audit session configuration for a fault that is the weekend. `observed` now carries
the reason on failure and stays clean on success; three tests pin it, one of which
sweeps a whole week asserting no failing gate ever reports an observed value its own
threshold lists as acceptable.

### §36 performance — measured, within budget

`scripts/perf_profile.py`, 400 instants over 30,000 M1 bars. Budgets are the engine's
own cadence, not invented numbers: the scalp scanner's is its 2-second scan interval.

| stage | p50 | p95 | p99 | max | budget | |
|---|---|---|---|---|---|---|
| market snapshot | 6.0ms | 31.2ms | 42.3ms | 70.0ms | 5000ms | OK |
| micro snapshot | 9.1ms | 15.0ms | 17.6ms | 21.8ms | 2000ms | OK |
| scalp cycle | 0.1ms | 0.3ms | 0.6ms | 1.0ms | 2000ms | OK |
| **full instant** | **16.1ms** | **42.5ms** | **58.7ms** | **79.7ms** | 5000ms | OK |

p99 is 58.7ms against a 2-second scan interval — roughly 34x headroom. Memory: **63.6 MB
flat across all 400 instants, +0.0 KB/instant after warm-up.** No leak signature.

The tail is what matters and it is measured rather than averaged away: a scanner whose
p99 exceeds its own interval stops being continuous without ever reporting an error.

## Still to do
- §39 profitability — **NOT POSSIBLE HERE**, needs the operator's harvested history
- Everything in the environment-limits table above, which needs a Windows machine with
  MT5 attached
