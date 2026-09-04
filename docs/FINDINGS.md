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

---

## 20. Operations that assumed an operator who likes terminals

Every operational path in this system ran through the CLI: setup, starting the three
processes, the pre-flight check, backtests, and the validation gate. That was never a
stated requirement — it was an assumption inherited from the fact that the system was
built by someone typing commands.

It matters more here than in most projects. The gate that decides whether a strategy may
ever touch real money is `xauusd validate`. If running it means opening a terminal, the
temptation is to skip it and trust the backtest already run — which is precisely the
failure the gate exists to prevent. A control that is inconvenient to exercise is a
control that gets skipped.

Three of those moved into the dashboard's System tab as background jobs with streamed
output. Two constraints shaped the implementation, both because this starts processes on
behalf of a network request:

- **The job list is a closed allowlist.** A caller names a job from a fixed set. There is
  no path by which a request supplies a command, an argument name, or a path.
- **Parameters are integers with stated bounds**, clamped and passed as separate argv
  entries with `shell=False`. A string cannot become an argv entry at all — the schema
  rejects it before the runner sees it, and the runner would fall back to the default
  anyway. There are tests for `"; rm -rf /"` and `"$(whoami)"` as parameter values.

Jobs are serialised, because two heavy runs competing for the same cores makes both
slower and the machine unresponsive.

**What deliberately did NOT move:** live arming. It is key 2 of a two-key design, and the
point of a second key is that it travels a different channel from the first. A button in
a web UI reachable from a phone would collapse both keys into one, leaving the two-key
arming as theatre. It stays a typed confirmation at the machine.

**A note on what is and is not verified.** The Windows launchers were written on Linux
with no Windows machine available, and shipped labelled as unexecuted. They have since
been run: `Setup.bat` (on the no-Docker path), `make-shortcuts.ps1`, `start.vbs`, the
dashboard and the System-tab jobs all worked first time, with the pre-flight check
passing. `stop.vbs`, `Arm Live Trading.bat` and the MT5 bridge remain unexercised.

Two things are worth keeping from that. First, the reason it worked is probably that the
fragile part of setup was moved *out* of batch and into `config/bootstrap.py`, which is
tested: parsing `.env` with `findstr` to decide whether a key already has a value is both
unreadable and untestable, and getting it wrong either destroys a working credential or
generates a second one nothing reads. What remained in batch was thin enough to get right
by inspection.

Second, shipping it labelled "not executed" cost nothing and was worth doing anyway. An
untested component that works is indistinguishable, from the outside, from a tested one —
right up until it doesn't.

---

## 21. The bridge ignored the credentials the setup file asks for

`.env.example` asks the operator for `XAUUSD_BROKER__LOGIN`, `__PASSWORD`, `__SERVER`
and `__TERMINAL_PATH`. `cmd_bridge` read none of them:

```python
serve(host=args.host, port=args.port, terminal_path=args.terminal_path,
      login=args.login, password=args.password, server=args.server)
```

Those argparse defaults are all `None`, and the Start shortcut launches the bridge with
no flags. So all four configured values reached nothing.

What makes this worse than an inert setting is that it still appears to work.
`mt5.initialize()` with no login attaches to whatever session the terminal already has
open, so the bridge connects, `doctor` prints an account, and everything looks correct —
possibly against a different account than the one configured. "Connected, plausibly, to
the wrong account" is a considerably worse failure than "not connected".

The bridge now falls back to the configured broker for anything not passed as a flag,
and says which it used. Attaching to the terminal's open session is still allowed — it
is the normal case for a manually-logged-in terminal — but it is now announced rather
than assumed.

**Class of bug:** the same one as finding 19, in a different place. A setting the
documentation asks for, read by nothing. Both were found by tracing what a documented
instruction actually reaches rather than by testing whether the code does what it says.
Worth doing for every value in `.env.example`: who reads this, and what happens if
nobody does?

---

## 22. Setup could not finish without Docker, while saying it could

`Setup.bat` told the operator, when Docker was absent:

> The system will fall back to a local SQLite database, which is fine for paper trading.

No such fallback existed. `.env.example` set

    XAUUSD_DATABASE__URL=postgresql+psycopg://xauusd:CHANGEME@localhost:5432/xauusd

and — since environment values now correctly override YAML (finding 19) — that URL beat
the SQLite default in `dev.yaml`. So on a machine without Docker the very next setup
step, `alembic upgrade head`, tried to reach a PostgreSQL that was never started, failed,
and setup exited. The message describing the fallback was the only place the fallback
existed.

Two mistakes compounded here, and the second is the interesting one:

1. A message describing behaviour nobody implemented.
2. **Fixing finding 19 created this.** While YAML outranked the environment, the
   placeholder URL in `.env` was inert and `dev.yaml`'s SQLite won; the setup message
   was accidentally true. Correcting the precedence made the placeholder load-bearing.

`.env.example` no longer pins a database URL at all. Unset, the config layer decides:
`dev` uses a local SQLite file that needs nothing installed, `demo` and `live` use
PostgreSQL. Setup writes a URL only when it has actually started PostgreSQL, which is
why the Docker check now runs *before* `.env` is written rather than after — what is
running determines what belongs in the file.

**Class of bug:** a correct fix reaching into a place nobody re-examined. The precedence
change was right and tested; what went untested was every value that had been quietly
depending on being ignored. Worth asking after any change to resolution order: what was
previously inert, and is it now live?

---

## 23. The file every document points at was never opened

An operator filled in `.env` exactly as instructed — broker login, password, server,
terminal path, and `XAUUSD_ENV=demo` — restarted, ran the pre-flight check, and got:

    config : env=dev mode=PAPER
    broker : OK (kind=sim) login=0 equity=10000.00 USD

    READY

`READY`. Green. Entirely wrong, and nothing on screen connected the correct file to the
ignored settings.

Two independent bugs, either of which alone was enough:

**1. `Settings.model_config` had no `env_file`.** Without it pydantic-settings does not
open `.env` at all. There is no warning for this — the file is simply never read, values
fall through to the config files, and everything looks normal. Every piece of
documentation in this repository (`.env.example`, the deployment runbook, the setup
guide) tells the operator to put their credentials there.

**2. `load_settings` chose the YAML layer from `os.environ` alone.** Even once `.env`
was being read, `XAUUSD_ENV=demo` in it set `settings.env = "demo"` while the loader
still layered `dev.yaml` — because which file to layer is decided *before* Settings
exists. So `demo.yaml`, the file that switches the broker from `sim` to `mt5_grpc`, was
never loaded, and the engine stayed on the simulator while reporting `env=demo`.

Why this survived every test: the precedence work in finding 19 was verified by setting
real environment variables (`XAUUSD_DATABASE__URL=... python -c ...`), which exercises
`env_settings` and never touches `dotenv_settings`. The tests were right about what they
tested. Nothing tested the path the documentation actually describes.

Beyond fixing both, `doctor` now states whether `.env` was found, its resolved absolute
path, and how many settings were read from it. The original failure was not that the
file was ignored; it was that a confident report gave no way to discover it.

**Class of bug:** a configuration source that fails open into plausibility. A missing
file, a refused connection, a malformed value — all announce themselves. A source that
is silently never consulted produces a system that runs perfectly on the wrong settings.
Worth asking of any config mechanism: if this input were ignored entirely, what would I
see? If the answer is "a normal-looking startup", something has to say otherwise.

---

## 24. Fixing finding 23 detonated a mine that finding 22 had already identified

Setup failed at the schema step with a psycopg connection timeout to localhost:5432 — on
a machine with no PostgreSQL, which the operator had been told they did not need.

The chain:

1. An early version of `bootstrap` wrote `XAUUSD_DATABASE__URL=postgresql+psycopg://...`
   into `.env` unconditionally.
2. Nothing read `.env` (finding 23), so that line was inert. Everything worked.
3. Finding 22 removed the placeholder from `.env.example` and made setup write a
   PostgreSQL URL only when it had actually started PostgreSQL — but only for `.env`
   files created *after* that change. An `.env` already on disk still had the line.
4. Finding 23 made `.env` actually load. The dormant line came alive.

Finding 22 closed with: *"Worth asking after any change to resolution order: what was
previously inert, and is it now live?"* — and the answer, missed, was sitting in a file
this project had itself written onto the operator's disk. Fixing the mechanism is not the
same as fixing the artefacts the broken mechanism produced.

A second, independent cause was underneath it: `demo.yaml` pinned PostgreSQL, so even a
clean `.env` made demo mode require Docker. Demo trading against an MT5 demo account has
no such dependency; `demo.yaml` now uses a SQLite file and setup points it at PostgreSQL
only when one is running. `live.yaml` still pins PostgreSQL, deliberately.

Both are now guarded rather than merely fixed. `bootstrap --check-database` runs *before*
alembic and:

- confirms the configured database answers a `SELECT 1`;
- if it does not, and the URL is one matching the template **we** generate, comments the
  line out (leaving the original visible) and continues on the local default;
- if the URL is anything else — a different host, port or database — reports the failure
  and stops, because a URL someone typed represents a decision that outranks our guess.

**Class of bug:** a fix that changes which inputs are consulted, applied to a system whose
existing state was written under the old rules. The migration was the work, and the code
change only looked like the work. Fixing a config mechanism means asking what the broken
version already wrote, and where.

---

## 25. Two installations, and a report that could not tell you which one you were reading

An operator sent three screenshots at once: a `.env` being edited under
`OneDrive\Documentos\...`, a setup traceback from `Downloads\...`, and a pre-flight report
showing `env=dev`, `kind=sim` and the same config hash as their very first run days
earlier.

Three artefacts, three different installations. They were editing one copy, running setup
in a second, and reading a dashboard served by a third — the original, still running, on
code from before any of the fixes. Every diagnosis drawn from that report was worthless,
and nothing in it said so.

This is an ordinary thing to end up with. Unzip once, re-download later, and a machine has
two complete copies with separate `.env` files, separate databases and separate Python
environments. Nothing about the pre-flight output distinguished them: it named the config
environment, the database URL and the broker, but never the folder any of it came from.

`doctor` now leads with the install path and version:

    install : C:\xauusd
    version : 0.1.0
    config  : env=demo mode=DEMO hash=...

Both lines exist for the same reason as the `.env` line added in finding 23: a report
that is confidently wrong is worse than one that fails. The path answers "which copy is
this?" and the version answers "is this the code I just installed?" — and until both are
right, no other line in the report means anything.

**Class of bug:** diagnostics that describe state without identifying their subject. A
report that cannot be attributed to a specific installation cannot be trusted at all once
more than one exists, and more than one always eventually exists.

---

## 26. The file we ship was the file that broke startup

Setup got as far as the new database check and then died inside pydantic:

    broker.login
      Input should be a valid integer, unable to parse string as an integer
      [type=int_parsing, input_value='', input_type=str]

`.env.example` ships blank keys on purpose — they show an operator which settings exist
and where to type. `XAUUSD_BROKER__LOGIN=` is one of them. For as long as nothing read
`.env` (finding 23) that blank was invisible. The moment the file was actually read, an
empty string arrived at an `int | None` field, and pydantic — correctly — refused it.

So the shipped example file could not be loaded by the code that shipped with it. Every
`str | None` key tolerated the blank; the one integer key did not.

The fix is a `mode="before"` validator on a shared `ConfigSection` base (and on `Settings`
itself) that **drops keys whose value is an empty string**. Dropping rather than coercing
is what makes a blank behave like a line that was never there: the field's default
applies, and a genuinely required field still reports itself as *missing* rather than
*malformed*. Only `""` counts as unset — `0` and `false` are real values, and there is a
test saying so.

This is the third consequence of finding 23, after 24 and 25, and the pattern across all
three is the same: a config source that had never been consulted was switched on, and
everything that had been quietly depending on being ignored surfaced at once — a stale
URL, a second installation, a blank line in the template. None of them were new bugs. They
were all pre-existing state that only became reachable.

**Class of bug:** shipping an example file that no test ever loads. `.env.example` is
executable configuration, not documentation, and it now has a test that loads it exactly
as an operator would. Worth asking of any template a project ships: does anything actually
run it?

---

## 27. The new database check could not open a database

Setup got past the blank-line fix and stopped on:

    database: sqlite:///data/xauusd_dev.db is not reachable (OperationalError)

SQLite, on a local file, on a machine with nothing else to blame. The cause was that
`data/` did not exist: it holds only generated files, so it is not in the repository, and
a fresh install therefore has no such directory. SQLite will not create a database file
in a directory that is not there.

The project already solved this. `database.session.make_engine` creates the parent
directory for a file-backed SQLite URL, and has since the beginning. But `check_database`
— written the previous day, for finding 24 — called `sqlalchemy.create_engine` directly:

```python
engine = create_engine(url, connect_args={"connect_timeout": 5} if "postgres" in url else {})
```

Reimplementing engine construction to add one connect argument silently dropped every
other thing the real constructor does. The diagnostic added to prevent an unhelpful
database error became the source of one.

`alembic/env.py` uses `engine_from_config` and had the same blind spot. It worked only
because setup happens to run the check first, which created the directory as a side
effect — an ordering dependency nobody wrote down and nothing tested. It now ensures the
directory itself.

**Class of bug:** a helper reimplemented instead of reused, for a reason that turned out
to be incidental. The connect timeout could have been a parameter to `make_engine`; going
around it instead discarded the accumulated knowledge in the function — which is exactly
what a shared constructor is for. Worth asking when writing `create_engine`, `open`, or
any other primitive: does this codebase already have a wrapper for this, and what does it
know that I am about to forget?

---

## 28. A token nobody asked for, guarding a page only its owner could reach

The first thing an operator saw after a successful install was a browser prompt:
*"This dashboard requires an access token."* No indication of what token, or where it
came from.

Setup generated one. `DashboardConfig` says, in its own docstring, that a token is
unnecessary on loopback because the OS is the boundary — and then setup created one
anyway, and the middleware enforced it, so a page reachable only from that machine was
password-protected by a secret the operator had never been told existed.

Setup no longer generates a dashboard token. A non-loopback bind still *requires* one
and is refused at startup without it, which is the guardrail that actually matters; an
operator who wants remote access sets a token then. A token already present is respected
untouched. The browser prompt, for anyone who has one, now names the file and the key
rather than asking for a secret in the abstract.

**Class of bug:** a security control applied where the threat model says it is not
needed, whose only effect is friction on the legitimate user. The bind check was the
real control all along; the generated token was cargo.

---

## 29. "DEGRADED", with no indication of what was degraded

First successful MT5 connection, and the pre-flight said:

    broker : DEGRADED (kind=mt5_grpc) login=5055396348 equity=1000.00 USD

`BrokerHealth.is_ok` is `connected and trade_allowed and trade_expert`. Three unrelated
conditions with three unrelated fixes — the terminal is offline, the account cannot
trade, or the AutoTrading button is off — collapsed into one word that distinguishes
none of them. `doctor` now names the failing flag and what to do about it.

The same report also carried:

    symbol spec : contract=100.0 tick_value_loss=0.1

The deployment runbook has, from the beginning, instructed the operator to *read those
numbers*. But the line omitted `tick_size`, without which the numbers cannot be read: a
tick value of 0.10 is correct for a broker quoting gold to three decimals
(100 × 0.001 = 0.10) and wrong by a factor of ten for one quoting two
(100 × 0.01 = 1.00). Identical output, and in one case every position size is ten times
too large.

So the check is now arithmetic rather than an instruction. `doctor` prints `digits` and
`tick_size`, computes `contract_size × tick_size`, compares it against the broker's own
`tick_value_loss`, and **fails the pre-flight** on a mismatch with "do not trade until it
is explained". A spec the system cannot size against is not a warning.

**Class of bug:** documentation standing in for a check. "Read those numbers and make
sure they look right" is a reasonable thing to write and an unreasonable thing to expect,
particularly when the two cases differ only in a field the report did not print. If a
document tells an operator to verify something mechanical, the machine should be
verifying it.

---

## 30. The cross-check that answers "is this tick value real?" was never called

A first live connection reported `tick_value_loss=0.1` against `contract_size=100.0`.
Whether that is correct depends entirely on tick size — 0.10 is right for gold quoted to
three decimals and ten times too small for two — and every position size in the system
derives from it.

The system was built for exactly this. `PositionSizer.calculate` takes
`broker_calc_profit`, the broker's own answer to "what does one lot lose between these
two prices?", compares it against its own arithmetic, and returns
`approved=False, reason="...refusing to trade on a specification we cannot verify"` when
they disagree beyond `sizing_cross_check_tolerance`. It has a unit test. It works.

`DecisionPipeline` called `risk_gate.evaluate(...)` without that argument. It defaulted
to `None`, the cross-check skipped itself, and the single defence against an incoherent
symbol specification did nothing for the entire development of the project — including
through the full backtest, the validation suite, and the parity test.

This is finding 17 again in a different place, and the resemblance is the point:

- **Finding 17:** the dashboard's HALT/FLATTEN wrote to a queue nothing read.
- **Here:** the sizer reads a parameter nothing wrote.

Both are a complete, correct, tested implementation of one half of a contract, with no
counterpart. Both look finished from either end. Neither had a test that crossed the
boundary — the sizer's test supplied `broker_calc_profit` directly, which proves the
comparison works and says nothing about whether anyone performs it.

`EngineState` now carries a `calc_profit` callable, the orchestrator supplies the
broker's, and the pipeline passes the result on every sizing. A broker that cannot answer
still yields `None`: the cross-check is corroboration, not a precondition, and an
unavailable broker must not stop the engine evaluating. Disagreement is what refuses the
trade, and that is now reachable.

**Class of bug:** a test that stops at the seam. Every unit test here passed because each
side was exercised with its counterpart hand-supplied. The question worth asking of any
optional parameter that carries a safety property: what test would fail if nobody ever
passed it? If the answer is "none", nobody does.

---

## 31. Four fake rejection reasons on every idle cycle

The first live decision journal, from a real MT5 connection on an idle market:

    NO_TRADE · no candidate

    REASONS AGAINST
      htf_conflict: no plan
      min_rr: None
      stop_validity: no plan or spec
      premium_discount: no plan

None of those are reasons a trade was rejected. They are four gates that judge a
*specific plan* being run with no plan, each reporting that it had nothing to judge.
`min_rr: None` in particular reads as though the 1:2 floor blocked something.

`_no_candidate_decision` ran the full `MANDATORY_GATES` list and reported every failure
except `has_candidate` as an "environment block". Its docstring states the purpose
exactly right — *"without this the rejection ledger cannot distinguish 'the market
offered nothing' from 'a filter is broken and silently rejecting everything', which is
the single most useful thing to know during paper trading"* — and then buried that
distinction under four entries of noise on every single idle cycle. The system is
designed to be idle most of the time, so the noise would have dominated the ledger
completely.

The gates now split explicitly. `ENVIRONMENT_GATES` (16) answer "is this a moment worth
trading in at all?" and are meaningful with no candidate. `PLAN_GATES` (5) judge a
specific plan. `MANDATORY_GATES` remains both, in the original evaluation order, so
nothing changes for a cycle that *has* a candidate. The no-candidate path runs only the
environment set.

Over 40 idle cycles the ledger went from four noise entries each to zero, leaving a
distribution that reads: 28 "environment is tradable but no strategy found a valid
setup", 12 "session". That is exactly the shape the runbook tells an operator to look
for, and it was previously unreadable.

There is a test asserting no `ENVIRONMENT_GATES` member short-circuits on a missing plan
— checked against the gate's own source — so the split cannot silently rot as gates are
added.

**Class of bug:** a correct intention, undermined by the implementation of the very
feature meant to serve it. The docstring and the code disagreed, and only the docstring
was right. Worth checking when a function explains what it is for: does it do that, or
does it do something adjacent that happens to include it?

---

## 32. The green light meant the web server was alive

The dashboard's status indicator read **"engine connected"**, in green, next to a green
dot. It is the one thing an operator glances at to know the bot is running.

It reported whether the browser's `fetch('/api/state')` had succeeded.

`/api/state` answers `200 OK` unconditionally. When no engine state has been published it
returns a body that says so — `{"connected": false, "message": "engine has not published
state yet"}` — and the front end ignored the body entirely:

```js
state.data = await api('/api/state');
setConnected(true);
```

So the light reported the health of the *dashboard's own web server*. Those are separate
processes. A crashed engine, a stopped engine, an engine that never started — all showed
green, indefinitely, because the thing being measured was still answering.

Worse, `hub.latest` is only populated by `POST /api/publish`, and nothing in the engine
ever calls it. The state the light was nominally about had never once been published in
the entire life of the project. This is the third instance of the same shape, after
findings 17 and 30: a producer and a consumer, each complete, never introduced.

Liveness is now derived from the decision journal instead — deliberately, rather than by
adding a heartbeat. The engine journals a decision on every M5 close whether or not it
trades, so a recent decision proves a **full cycle completed**: data fetched, gates run,
outcome recorded. A heartbeat proves only that a thread is alive, which an engine can be
while doing nothing useful. Two intervals of grace absorbs one slow broker call; two
missed in a row is a stopped engine.

`/api/health` uses the same function, so the two can no longer disagree.

**Class of bug:** a status indicator measuring its own transport. The question worth
asking of any health display: if the thing it describes died right now, what would this
show? If the answer is "the same as before", it is not a health indicator.

---

## 33. The broker's own spec was internally inconsistent, and sizing read the wrong half

MetaTrader's Specification dialog for XAUUSD on a MetaQuotes demo server:

    Digits          2
    Contract size   100
    Tick size       0.01
    Tick value      0.1
    Calculation     CFD Leverage

100 × 0.01 = **1.00**. The broker reports **0.10**. Two fields describing the same
quantity, ten times apart, from the same dialog.

`PositionSizer` uses `tick_value_loss`, via
`value_per_price_unit = tick_value_loss / tick_size`. On this spec that yields $10 per
lot per dollar of gold movement; the contract says $100. **Every position would have been
sized ten times too large — a 1% risk placed as 10%, and the 2% daily drawdown limit
breached by a single stop-out.**

Three things are worth separating here.

**The system read a field it was right to read.** `CLAUDE.md` requires that broker specs
are "always read, never assumed", and `tick_value` is the correct field: it accounts for
cross-currency conversion that `contract_size × tick_size` does not. The failure was not
choosing the wrong field. It was trusting a single field with no corroboration for a
number that determines every position size.

**Guessing which half is right would be the same mistake.** Silently preferring
`contract_size × tick_size` would be assuming a spec rather than reading it, and would be
wrong for any symbol whose profit currency differs from the account currency. The system
refuses instead: pre-flight reports NOT READY, and the sizer's cross-check (finding 30 —
only wired the day before, and this is the first thing it caught) declines the trade.

**The tie is broken by measurement, not by preference.** `doctor` now asks the broker to
price a one-tick move on one lot via `OrderCalcProfit` and prints which of the two
candidates it matches. That is the arithmetic the money actually follows, so a
descriptive field being wrong is discoverable rather than merely suspicious.

MetaQuotes demo servers ship generic instrument definitions; a real broker's demo does
not usually have this defect. But the point is not this server. It is that a spec can be
internally inconsistent, the wrong half can be the one sizing reads, and nothing about
the resulting behaviour looks abnormal — the bot places orders, they fill, the numbers
are simply all ten times too big.

**Class of bug:** trusting a single unverified input for a quantity where being wrong is
unbounded. The check costs one broker call at startup. Not doing it costs the account.

---

## 34. The pre-flight checked a symbol the engine would never use

A broker whose Market Watch shows `GOLD, H1: SPOT Gold Ounce vs US Dollar` produced:

    broker : OK (kind=mt5_grpc) login=592040268 equity=1000.00 USD
    broker : FAILED — RuntimeError: symbol_select failed for XAUUSD

The bridge was connected and the account was live. The failure was that `doctor` called
`broker.symbol_spec(settings.symbol)` — the literal `XAUUSD` from the config file.

The engine does not do that. `TradingEngine._resolve_symbol` asks the broker for its
symbol list and runs `resolve_symbol`, which matches `^XAU` or `^GOLD`, filters to
tradable USD instruments, and picks by spread and quality. It would have found `GOLD`
immediately.

So the pre-flight — whose entire purpose is *verify before running* — exercised a
different code path from the thing it verifies. On this broker it failed where the engine
would have worked. The reverse case is worse and was equally possible: a broker offering
both `XAUUSD` (untradable, wide-spread, or a CFD on a different underlying) and
`XAUUSD.pro` would have had the pre-flight bless the configured name while the engine
traded the other one, with every check — spec coherence, tick value, quote sanity —
performed against an instrument nobody was going to trade.

`doctor` now resolves the symbol the same way and prints the result, including when it
differs from the configured name:

    symbol resolved  : GOLD  (config says XAUUSD) — SPOT Gold Ounce vs US Dollar

and every subsequent check — spec, tick value, `calc_profit` — uses the resolved name.
A resolution failure now says which patterns were tried and points at
`XAUUSD_DATA__SYMBOL_OVERRIDE`.

**Class of bug:** a verification step that does not use the code path it verifies. It is
the same family as testing at the seam (finding 30): both sides work, the check passes,
and nobody exercised the join. A pre-flight is only worth its output if it fails exactly
when the real thing would.

---

## 35. A coherent spec that might be the wrong instrument entirely

The second broker's `GOLD` specification is internally consistent — contract 100,
tick size 0.01, tick value 1, and 100 × 0.01 = 1.00. The check from finding 33 passes,
and sizing on this broker will be correct.

The rest of the same dialog:

    ISIN        US00181T1079
    Exchange    XNYS
    Sector      Basic Materials
    Industry    Gold
    Country     United States

An ISIN, a New York Stock Exchange listing, a sector and an industry. Those are equity
attributes. Spot gold has no ISIN and is not listed on the NYSE. `GOLD` is also the NYSE
ticker for a gold mining company, and a broker offering both spot metal and share CFDs
can easily have one symbol whose description says one thing and whose metadata says
another.

Nothing in the tradeable numbers distinguishes them. Contract size 100, tick size 0.01
and tick value 1 describe *100 ounces of bullion at $0.01 increments* and *100 shares at
$0.01 increments* equally well. Both are coherent. Both size correctly. Only one is the
instrument the strategy was designed for, and a Smart-Money-Concepts model of liquidity
sweeps and session ranges applied to a mining company's shares is nonsense that would
still produce confident-looking trades.

The price separates them absolutely: bullion is thousands of dollars an ounce, the shares
tens. `sanity_check_quote` has always enforced a 400–20,000 band, and the engine calls it
at startup — but `doctor` did not, which is finding 34 again in the same function, one
line further down. The pre-flight now prints the quote and applies the same check, for
real brokers only (the simulator legitimately has no market data until a backtest loads
some, and failing on that would report the simulator's emptiness as a broker fault).

**Class of bug:** validating a thing's properties without validating its identity. Every
number was checked and every number was right, for an instrument that might not be the
one anybody meant to trade. Worth asking after any consistency check passes: consistent
*with what*, and is that the thing I think it is?

---

## 36. The only backtest an operator could run was the one guaranteed to find nothing

A backtest over 30,000 synthetic bars reported `0 trades`, and the rejection ledger
accounted for every one of its 2,660 decisions as `NO_CANDIDATE` (1,452) or `session`
(1,208). Read as a verdict on the strategy, that is alarming. It is not a verdict on
anything.

Instrumenting the confluence chain link by link over the same data: of 560 direction
attempts, 280 died on higher-timeframe conflict (expected — one of two directions
always conflicts), 100 on the sweep links, and **all 180 that reached the market
structure shift died there.** Not one attempt ever reached the FVG link. The material
was present — sweeps on 99% of instants, FVGs on 100%, order blocks on 99.8% — but an
MSS requires a directional bias established by a BOS and then broken *against* with
≥0.75 ATR of displacement, and a random walk does not produce that in coordination with
a liquidity sweep 15 bars earlier. The chain is intact; the data has no structure for it
to find.

The suite already said so. `test_no_edge_data_produces_no_trades` asserts exactly this
result. Synthetic data is a plumbing smoke test and the CLI prints "results are
meaningless as a trading result" every run.

The actual defect was upstream of all of it. `Broker.bars` could always fetch history
and `BarRepository.upsert_many` could always store it, and **nothing ever introduced
them**. There was no `harvest` command, no job, no script — the backtest's own error
message told the operator to "Harvest history first" against a producer that did not
exist. And `BacktestJob` hardcoded `--synthetic`, so the dashboard button could not have
run real data even if the database were full. The one backtest reachable from the
product was the one the test suite asserts finds nothing.

So "0 trades" was reporting the absence of data while looking exactly like the absence
of edge, and the two demand opposite responses: one is fixed by downloading history, the
other by changing the strategy. An operator who read it the second way would have gone
looking for thresholds to loosen — which is the one change this system must never make.

**Class of bug:** the fifth producer/consumer pair in this project where both halves were
complete, tested, and never connected (after HALT/FLATTEN, `calc_profit`, state
publishing, and engine liveness). The tell is the same every time: an error message or a
docstring that names a step no code performs. Grep the imperatives in user-facing strings
and check that something implements each one.

**Second-order lesson:** a smoke-test fixture became the default path to a headline
number. `synthetic` now defaults to 0 (real history) in the job catalogue, so the easy
button gives a meaningful answer or an honest complaint about missing data — never a
confident-looking zero.

---

## 37. Two scalp models could not fire, and it looked exactly like a quiet market

The first scan of the new scalp engine over 998 instants: `sweep_reversal` 14 signals,
`breakout_retest` 6, `momentum_continuation` 4, and **`fvg_retracement` 0,
`ob_reaction` 0.**

Two of five models silent is easy to explain away — those patterns are rarer, the
synthetic fixture is thin, the proximity filter is tight. All plausible, all wrong.

Instrumenting the funnel inside those two models:

```
fvg_tradable        381      gaps of the right direction, unmitigated
fvg_displacement    381      all of them clear the displacement threshold
fvg_near             79      price is close enough to trade
fvg_stop_ok           0      <-- every one rejected
fvg_stop_too_tight   79
```

Not one candidate in seventy-nine failed on the market. They failed on geometry. The
entry was placed at the gap edge *nearest invalidation* and the stop just beyond that
same edge, so the stop distance was the buffer alone — a constant `0.30 × ATR`, against
a configured floor of `0.80 × ATR`. The model could not produce a valid signal at any
price, on any data, ever. Same bug, same line, in the order-block model.

The reasoning that produced it was superficially sound and written into the docstring:
"entry sits at the gap edge nearest invalidation — the whole point is a known, close
stop". True as far as it goes, and it forgot that the stop has to sit somewhere *past*
the invalidation, which meant entry and stop collapsed onto the same level.

Corrected, entry goes at the edge price reaches *first* on the retrace — the
conservative fill, since a limit deeper in the gap only fills if price traverses the
whole thing — and the stop sits beyond the far edge. Risk is then the zone height plus
a buffer, which is what it should always have been. Signals went from 24 to 57 and both
models began firing.

**Class of bug:** a component that is complete, imports cleanly, is called on every
cycle, and is structurally incapable of producing output. It cannot be distinguished
from correct-but-quiet by watching it, only by instrumenting it. This is the sixth
variant in this project of "looks finished from the outside" — after four
producer/consumer pairs and one verifier that used a different code path from the thing
it verified.

**The guard:** `tests/unit/test_scalp_models.py` asserts each model fires at least once
over the scan, with a message saying explicitly that a silent model and an absent
pattern need opposite responses and the test must be investigated rather than relaxed.

**Second bug, found by a test written for the first.** `ScalpScorer` clamped factors
with `max(0.0, min(1.0, x))`. In Python `min(1.0, nan)` returns `1.0`, so a NaN factor
scored **full marks** — warm-up would have inflated a score rather than suppressing it,
in the one place where a number decides whether to risk money. Degradation is supposed
to be one-directional everywhere in this system; here it was inverted. `clamp01` sends
NaN to zero, and a test pins it.

---

## 38. The scalp engine could trade live but could not be backtested

The first real-data backtest, on 39,992 harvested M1 bars:

```
0 trades
Rejection ledger:  NO_CANDIDATE 312 | score_a 10 | session 10 | spread 6
```

Every one of those is an A/A+ gate name. `grep -c scalp src/xauusd/backtesting/engine.py`
returned zero: `BacktestEngine` drove `DecisionPipeline` and nothing else. The scalp
engine had been wired into the live orchestrator the commit before, so the state of the
system was **the scalp engine can reach real money but cannot be validated** — exactly
inverted, and it silently disabled the RR sweep, walk-forward, Monte Carlo and the 65%
bound, which is the entire apparatus standing between "it trades" and "it should trade".

Seventh instance of the same class in this project.

**And behind it, a fourth copy of one number.** Wiring the backtester in produced
`risk_approved ... classification=SCALP rr=1.5` followed by zero trades. `_execute`
re-checks reward-to-risk at send time against `thresholds.min_rr`, which is 2.0. So the
risk gate approved a 1.5R scalp and the execution step refused it one line later.

That check existed in four places, each individually correct for the engine it was
written for:

| Where | What it guarded |
|---|---|
| `g_min_rr` | the A/A+ plan gate — correct, scalps never run it |
| `RiskGate.evaluate` | fixed one commit earlier |
| `BacktestEngine._execute` | send-time re-check, backtest |
| `OrderManager.preflight` | send-time re-check, **live** |

The live one is the dangerous one: the wired engine would have approved scalps all day
and never sent a single order, and the rejection would have read "reward-to-risk fell to
1.50 (floor 2.0)" — which looks like a market problem, not a configuration one.

`Settings.min_rr_for(classification)` is now the only definition. A test walks the
source tree and fails if any file outside an allowlist reads `thresholds.min_rr`
directly, because a fifth copy would reintroduce this exactly.

**Class of bug:** one rule, four enforcement points, added at four different times for
four correct reasons. Nothing was wrong when each was written; the rule acquired an
exception later and only three of the four learned about it. Worth asking, whenever a
threshold gains a tier: *where else is this number read?*

After both fixes the scalp engine trades in a backtest — detect, score, economics,
correlation, risk, execute, close, recorded.

---

## 39. The higher-timeframe read was five points, and pointed at the wrong timeframes

The complaint that prompted this: *"the 5m did not take a single trade in a whole week,
it must analyze the 4h, 1h, 15m also to get precise entries."*

The system did read higher timeframes — the A/A+ engine reads six of them — but the
scalp tier's entire higher-timeframe input was one function:

```python
for tf, w in ((Timeframe.H1, 0.25), (Timeframe.H4, 0.35), (Timeframe.D1, 0.40)):
```

worth 5 points out of 100. Two things are wrong with it, and they are different kinds of
wrong.

**M15 was absent.** `MarketAnalyzer` builds M15 structure, and the setup timeframe for
every zone in the snapshot *is* M15 — the FVGs, order blocks and pools a scalp trades
against are M15 objects. The one place that asked "what do the higher timeframes think"
skipped the timeframe all of its own evidence came from.

**D1 outweighed everything.** The heaviest vote on a trade that closes inside ninety
minutes came from a bar that outlives it. A daily bias could suppress a signal whose
entire life fits inside one of its candles, and no amount of M15 and H1 agreement could
outvote it.

But the deeper error is that the read was **only a score**. Higher-timeframe context
changes two things about a trade and the old code addressed neither:

| What HTF should determine | What it did |
|---|---|
| whether the entry is at a level someone defends | nothing — `entry_location` was M1/M5 only |
| where the target can realistically reach | nothing — obstacles were M1/M5 pools and S/R |

The second is the one that costs money. `structural_target` pulled targets in behind
M1/M5 pools but happily aimed 1.5R straight through an H4 resistance. That is not a 1.5R
trade; it is a trade that stalls at the level and exits on the time stop, and the
backtest scores it as a small loss with no indication why.

`strategy/scalp/htf.py` now returns three separate things from one read — `alignment`
(M15 0.35, H1 0.30, H4 0.25, D1 0.10), `confluence` (is the entry standing on an HTF
FVG, order block, level, or the right half of the HTF dealing range), and `obstacles`
(HTF levels, resting pools and opposing zones *ahead* of the entry, fed into
`structural_target`). The factor is weighted 12 rather than 5, taken from liquidity,
momentum, volatility, session and dxy.

**Two invariants have tests because both are ways to cheat.** Obstacles can only pull a
target *in*, never push it out — otherwise a chart opinion becomes free reward-to-risk.
And removing a timeframe can never raise the factor: `test_dropping_a_timeframe_can_never_raise_the_factor`
walks every subset boundary, because a data outage that unlocks trades is the exact
shape of failure this system is built to refuse.

**What this does not do:** it does not make the strategy profitable, and it is not
evidence of an edge. Widening the read gives the scorer better information; whether
better information produces positive expectancy after costs is an empirical question
that only `scripts/scalp_sweep.py` on real harvested history can answer.

---

## 40. The scalp path could route an unvalidated strategy to real money

Found while checking, for an unrelated reason, where `live_eligible` is read:

```
$ grep -rn "live_eligible" src/ --include=*.py
src/xauusd/strategy/gates.py:213
src/xauusd/strategy/classifier.py:120
src/xauusd/domain/enums.py:389
```

Two call sites, both on the A/A+ path. `ScalpPipeline` was built as a deliberately
parallel route to the same broker — same `RiskGate`, same sizing cross-check, same daily
and weekly lockouts — and it ran five gates of its own without ever running that one. So
the system's actual state was:

| | status DEV, mode LIVE |
|---|---|
| A/A+ strategy | refused by `g_strategy_validated` |
| scalp model | **routed** |

Every scalp model ships DEV, because none has been validated. The whole apparatus that
stands between "it trades" and "it should trade" — out-of-sample split, walk-forward,
Monte Carlo, the deployment gate — expresses its verdict as a `ValidationStatus`, and
the newest path to the broker did not read it.

Nothing was wrong when either piece was written. `g_strategy_validated` correctly guards
the gate chain it belongs to; `ScalpPipeline` correctly delegates risk to the risk
module. The rule simply lives in the gate chain, and the scalp path is not a gate chain.

**Ninth instance of the class**, and the second (after FINDINGS 38's `min_rr`) where the
missing copy was the one on the live path. That is not a coincidence: the live path is
the least exercised by tests and the most recently connected, so it is where a rule is
most likely to be absent and least likely to be noticed.

`scalp_strategy_validated` now runs as **stage 0** of `ScalpPipeline._evaluate`, before
the score and before anything can approve. Deliberately first: a check that only runs
after four other gates pass is a check the failing case has never exercised. A model
with no database row reads as DEV — absence of evidence of validation is not evidence of
validity — and the result is recorded on the evaluation whether it passes or fails, so
the journal can show the check was consulted rather than merely not fired.

`tests/unit/test_scalp_live_eligibility.py` asserts the routing is *impossible* rather
than absent, including the case that matters most: a model whose signal is otherwise
flawless — clearing the score, the RR floor and both economic gates — is still refused.
A test whose signal could fail for another reason would pass whether or not the check
exists.
