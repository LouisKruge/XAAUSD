# Findings

Bugs and design problems found while building this system, and what each would have
cost if it had reached production. Recorded because the *class* of each bug is more
useful than the fix — every one is a mistake that is easy to make again.

---

## 1. Sortino inflated ~120× (most serious)

**Found by:** rendering the dashboard against real data and asking whether "Sortino
700.05" could possibly be true.

Sortino used the standard deviation of the **losing subset** as its denominator, rather
than the textbook downside deviation `rms(min(r − target, 0))` over **all** trades.

For a fixed-stop system this is not a rounding difference. Every loss is approximately
−1R by construction, so the losing subset has almost no dispersion:

```
real 12-trade sample:
  std(losses)          = 0.005   →  Sortino 700
  downside deviation   = 0.662   →  Sortino 5.2
```

**Cost if shipped:** the deployment gate requires Sortino ≥ 2.0. Every fixed-stop
strategy would have passed that criterion trivially and meaninglessly, including ones
the correct formula blocks. This is precisely the failure the gate exists to prevent.

**Class of bug:** a statistic that is *defined* for a distribution the system does not
produce. Worth re-examining every ratio whose denominator is a dispersion measure.

---

## 2. Every exit labelled `STOP_LOSS`

**Found by:** an 11-month backtest whose exit-reason breakdown was
`Counter({'STOP_LOSS': 5})` — including a trade that closed at **+1.03R**.

`SimBroker` labelled any exit at the stop price a stop-out, without checking whether the
stop had been *moved* to break-even or trailed into profit.

**Cost if shipped:** the exit-reason analytics would be useless, and worse, actively
misleading — a reviewer would conclude the system never reaches its targets. Since
break-even and trailing behaviour is exactly what one tunes from that breakdown, it
would have been tuned blind.

The fix uses a **risk-relative** tolerance rather than exact equality: the entry sits on
the far side of the spread, so an exact comparison never matches and everything stays
mislabelled.

---

## 3. `planned_rr` computed the realised move

`ClosedTrade.planned_rr` returned `|exit − entry| / risk` — the distance price actually
travelled, not the RR the plan targeted. It feeds `avg_rr_planned` in validation reports.

**Cost if shipped:** a system with a hard 1:2 floor reporting "planned RR 0.98". An
obviously impossible number, which is the good case; the bad case is nobody noticing and
the metric quietly meaning nothing. Split into `planned_rr` (from the plan) and
`realised_rr` (travelled), with both plus the payoff ratio shown separately.

---

## 4. A directional bias from 25 bars

The structure engine's minimum-history guard was `atr_period + 2·lookback + 2` = 20 bars.
With 25 bars of noise it would return `BULLISH` and that bias would propagate into the
HTF-alignment gate as though it were knowledge.

Raised to `max(3 × atr_period, 50)`. **Class of bug:** a guard that ensures a *calculation*
is defined, mistaken for one that ensures the *answer* is meaningful.

---

## 5. Scoring weights totalled 95, not 100

The example weights in the original brief sum to 95. Left alone, every score would have
been silently capped at 95 and thresholds calibrated against a scale nobody intended.
Caught by a validator that now enforces the total, so the error cannot recur.

---

## 6. A plan at exactly 1:2 can never fill

A setup planned at exactly the 1:2 floor is *always* rejected once the spread is applied,
because the fill happens on the far side of the quote.

Not a bug — correct behaviour — but a non-obvious consequence worth knowing: **strategies
must target above the floor, not at it.** Kept as an explicitly named test rather than
worked around.

---

## 7. Strategies emitted candidates the classifier always rejected

Strategies checked D1/W1/MN1 for higher-timeframe conflict; the classifier checked
MN/W/D/**H4**. Every H4-conflicting candidate was generated, scored, gated and rejected.

Wasted work, and worse, it polluted the rejection ledger — the screen used to diagnose
whether the bot is idle by design or by defect.

---

## 8. Performance: the decision cycle was 434 ms

Three separate problems, all found by profiling rather than guessing:

| Problem | Fix | Effect |
|---|---|---|
| `series.body_ratio` recomputed the whole array inside a per-bar loop | hoist | |
| Swing detection was a Python loop of `np.all` calls | sliding-window vectorisation | |
| Zone analysis re-ran every M5 bar though it only changes on an M15 close | cache on the setup bar | |
| | **combined** | **434 ms → 25 ms** |

---

## 9. LightGBM thrashes under container CPU limits

LightGBM defaults to one thread per core. Under a cgroup CPU limit (a VPS, or CI) this
causes severe thread contention: **42.7 seconds** for a 600-sample fit versus **0.02
seconds** with `n_jobs` pinned. Pinned deliberately, with the measurement recorded in the
code so nobody "optimises" it back.

---

## 10. Conventional red/green fails colourblind readers

The standard trading green/red pair measures ΔE 4.1 under deuteranopia — effectively
indistinguishable for roughly one man in twelve. Measured with a palette validator rather
than eyeballed, then replaced with a teal/coral pair measuring 11.8 that still reads as
up/down. Colour is never the only cue regardless: every direction carries a text label.

---

## 11. Swallowed dashboard errors

Every dashboard refresh had `catch (e) { /* ignore */ }`. A failed fetch produced an
empty panel, which is indistinguishable from "no data" — on a trading dashboard, the
difference between "the bot is being selective" and "the bot is broken". Failures now
render in the panel where the operator will see them.


---

## 12. A test that bypassed the gates it was meant to exercise

The trade-path integration test passed `gates_passed=True` into the classifier so it
could focus on scoring. That silently stopped checking every requirement enforced by a
**gate** rather than by the classifier — premium/discount location among them.

It surfaced only because a deliberately-weakened case (a long in premium) was still
classified `A`. The classifier is right not to duplicate the gate; the test was wrong to
stub it. It now runs `run_gates` for real.

**Class of bug:** a test helper that simplifies away the exact composition it is
supposed to be testing. Worth asking of any test fixture: which real component did I
replace with `True`?

---

## 13. Generating data and hoping it contains a setup

The original version of that test generated a random walk and asserted at least one
A-grade trade appeared. It failed repeatedly — not because the decision path was broken,
but because a random walk rarely contains the full confluence chain, which is the whole
point of the system.

Three attempts to hand-plant a setup were each *correctly* rejected: the first because a
14-point 5-minute displacement is roughly 15 ATR and the regime engine classified the
market ABNORMAL; the second because the retrace landed in premium; the third because the
deep pullback made H1 read bearish, conflicting with a long.

That difficulty is itself informative — it is a fair proxy for how rare a genuine A setup
is meant to be. The fix was to stop conflating two questions:

- *Does the decision path turn a valid setup into a sized trade?* → construct the
  snapshot directly. Fast, deterministic, and it tests the actual claim.
- *Does real market data contain such setups?* → the backtester answers this, and its
  answer was 5 trades from 24,064 evaluations over 11 months.

---

## 14. A guard evaluated after the thing it guards against

`compute()` in `backtesting/metrics.py` read:

```python
if rs.std(ddof=1) > 0 and len(rs) > 1:
```

Python evaluates left to right, so on a single-trade sample `std(ddof=1)` divides by
zero *before* the `len(rs) > 1` test that exists to prevent exactly that. It returned
`nan`, `nan > 0` was `False`, and the branch was skipped — so the output happened to be
correct, and the only visible symptom was a `RuntimeWarning` buried in the test log.

Correct output is not the same as correct code. The same guard written the other way
round is free:

```python
if len(rs) > 1 and rs.std(ddof=1) > 0:
```

**Class of bug:** a short-circuit guard placed on the wrong side of the operator. Cheap
to write, silent in production, and it only stays harmless while the accidental `nan`
keeps comparing false. There is now a test that runs `compute()` on one trade with
`RuntimeWarning` promoted to an error.

---

## 15. Three columns that lied about their own type

`DecisionRow.gate_trace` was declared `Mapped[dict[str, Any]]`. It stores a **list** —
its own column default is `list`, and the only writer is `d.gate_trace()`, which returns
`list[dict[str, Any]]`. `reasons_for`, `reasons_against`, `all_blocking`,
`approved_regimes` and `approved_sessions` were wrong the same way.

Nothing broke, because JSON columns accept whatever they are handed and both the CLI and
the dashboard iterate the value as a list. But the declared type was the opposite of the
truth, and anyone trusting the model — reasonable, since the model is the schema — would
write `row.gate_trace["something"]` and get a key error at runtime rather than a type
error at check time. Iterating a `dict[str, Any]` yields `str` keys, which is precisely
what mypy was complaining about in `cli.py`; the report pointed at the reader, but the
defect was in the model.

**Class of bug:** an annotation nobody rechecked after the value changed shape. Worth
noting that this was found by clearing *unrelated* type noise — see below.

---

## 16. Why the noise mattered

mypy reported 65 errors across the tree. Most were narrowing limitations and
`warn_return_any` against numpy, and it would have been reasonable to call all 65
cosmetic and move on. Finding 15 was one of those 65.

The suppressions were the same story: seven `pytest.raises(Exception)` blocks sat around
the risk-config validators — the tests whose entire job is proving the 1%/2% ceilings and
the drawdown ordering are enforced. A blind `Exception` there passes just as happily on a
typo'd keyword argument as on the validator firing, which means the test could have been
asserting nothing at all. They now assert `ValidationError` specifically, and the
idempotency test asserts `IntegrityError` rather than any exception.

The tree is now clean under both `ruff` and `mypy`, with four ruff rules disabled
explicitly and with reasons in `pyproject.toml` rather than by accretion. That is not
tidiness for its own sake: a checker with 65 standing errors is a checker whose 66th —
the real one — nobody sees.

---

## 17. A safety button wired to nothing

The dashboard's HALT and FLATTEN posted to `/api/commands/*`, which appended a dict to
`hub.commands` — a plain list on the `Hub` object in the API process. The engine runs in
a *different process* and never read it. Nothing did, except `GET /api/commands/pending`,
which returned the list and cleared it.

So the sequence was: operator hits FLATTEN in an emergency, the UI says
`"FLATTEN queued for the engine"`, and nothing happens. That is strictly worse than
having no button at all — a missing control sends you to the terminal, a fake one tells
you the account is being closed while it is not.

Three things were wrong at once and each needed a different fix:

- **Wrong medium.** Commands now live in `operator_commands` in the database, which both
  processes already share. A queued emergency stop survives a restart of either, and an
  instruction to close every position leaves a permanent record of who asked and what
  happened. Claiming is a status transition inside the transaction, so a redelivery
  cannot close a position twice.
- **No consumer.** The engine now has a `_command_loop`, deliberately separate from the
  M5 decision cycle so an emergency FLATTEN is not waiting behind a bar close.
- **Silent failure.** If any position fails to close, the command is recorded `FAILED`
  and a CRITICAL alert fires. The one thing this path must never do is report an account
  flat when it is not.

`FLATTEN` also trips the kill switch before closing. Flattening without halting invites
re-entry on the next M5 close, which is the opposite of what the operator asked for.

**Class of bug:** a feature whose two halves were written against an interface neither
end implemented. Both sides looked complete in isolation. Worth asking of any control
surface: what test proves the button reaches the thing it names?

---

## 18. An open control plane, one flag away

The dashboard had no authentication of any kind. It bound to `127.0.0.1`, and that alone
was carrying the entire security argument — including for `POST /api/commands/flatten`.

The worst of it was `GET /api/commands/pending`, which *consumed* the queue. An
unauthenticated reader could poll it and swallow the operator's emergency stop before the
engine ever saw it. A read endpoint that destroys state is a hazard on its own; one that
destroys safety state and needs no credentials is worse.

The fix is not a token check on the three endpoints that looked dangerous:

- Authentication is **middleware over `/api`**, not a per-route dependency, so a route
  added next month is protected because of where it lives rather than because its author
  remembered. The page shell stays open — a browser cannot attach a bearer header to a
  top-level navigation, and the shell holds no data.
- `DashboardConfig` **refuses to construct** with a non-loopback host and no token, and
  `run()` re-checks because `--host` reaches uvicorn without passing through the config.
  "Remember to set a token" is not a control; a bind that will not start is.
- Reads are guarded too. The decision journal is the record of a live trading account.
- The WebSocket takes the token as a query parameter, since middleware never sees a
  WebSocket scope and the handshake cannot carry a header.

**Class of bug:** a deployment assumption ("it's only on localhost") load-bearing for a
security property, in a component whose entire purpose is to be looked at from somewhere
else. The moment the user asks "how do I access this remotely", the assumption is gone
and nothing replaces it.

---

## 19. The environment variables the runbook tells you to set were ignored

`load_settings` merged `base.yaml`, `<env>.yaml` and `local.yaml`, then did:

```python
return Settings(**merged)
```

Passing the merged YAML as constructor arguments makes it **init state**, and in
pydantic-settings init state outranks the environment. So any key that happened to
appear in a YAML file silently ignored its `XAUUSD_*` variable, while any key that did
not appear worked fine. `broker.login` worked (not in `base.yaml`); `database.url` did
not (it is).

`database.url` is the one that matters. `.env.example` and the deployment runbook both
tell the operator to set `XAUUSD_DATABASE__URL` to their Postgres instance. Following
that instruction exactly, on a live account, the engine would have gone on writing the
decision journal and the position record to `sqlite:///data/xauusd.db` — no error, no
warning, and `doctor` would have printed the SQLite path without anyone reading it as
wrong, because it looks like a normal line of output.

The fix is not to reorder `init_settings` and `env_settings` wholesale — that would make
an explicit `Settings(mode=LIVE)` lose to an ambient variable, which is its own trap.
The YAML is now a real settings *source* (`YamlSettingsSource`) placed below the
environment, leaving the priority where a reader would expect it:

    explicit arguments  >  environment  >  .env  >  YAML files  >  defaults

**Class of bug:** configuration precedence that is invisible when wrong. Nothing raises,
nothing logs, and the value you see is a plausible one — just not the one you set. It is
worth testing precedence explicitly for any layered config, because no amount of reading
the loader tells you what the library does with what you hand it.

A related note: because the YAML layer is process-global (pydantic-settings builds its
sources from the class, not the call), `load_settings` restores the previous layer in a
`finally`. There is a test asserting the layer does not outlive its call.
