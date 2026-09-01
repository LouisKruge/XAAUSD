# Deployment Runbook

Stages 4–7 from `architecture/05-roadmap.md`, as operational procedure. Nothing here
is optional; the stages exist because each catches a class of problem the previous one
cannot.

---

## If you would rather not use a command line

Everything below has a click-driven equivalent on Windows. You need a terminal for
none of it.

**Once**, double-click `windows\Setup.bat`. It installs the Python environment, starts
the datastores, builds the schema, generates a dashboard token and a database password,
and puts three shortcuts on your Desktop. It is safe to run twice — every step checks
before it acts, and it never overwrites a secret that already has a value.

**After that**, three Desktop icons:

| Icon | What it does |
|---|---|
| **Start XAUUSD Bot** | Starts the bridge, the engine and the dashboard, then opens the dashboard. No console windows. |
| **Stop XAUUSD Bot** | Stops those three. Open positions are left alone — flatten them from the dashboard first if that is what you meant. |
| **Arm Live Trading** | Key 2 of the live arming. Deliberately a console prompt, see below. |

**Everything else is in the dashboard.** Its **System** tab runs the pre-flight check,
a backtest, and the validation gate as buttons, streaming the same output the terminal
would have shown. Decisions, the rejection ledger, performance, halt and flatten already
had their own tabs.

The one thing deliberately *not* in the dashboard is live arming. It is key 2 of a
two-key design, and the whole point of a second key is that it travels a different
channel from the first. A button in a web UI you can reach from your phone would collapse
both keys into one. It stays a typed confirmation at the machine.

Two honest caveats:

- The Windows launchers are **confirmed working through Phase 1** on a real machine:
  `Setup.bat` (including the no-Docker SQLite path), `make-shortcuts.ps1`, `start.vbs`,
  the dashboard, and the System-tab jobs. Still unexercised: `stop.vbs`,
  `Arm Live Trading.bat`, and the MT5 bridge path, since that needs a broker attached.
- `Setup.bat` shows a console window while it runs, because it needs to report progress
  and errors somewhere. You do not type into it.

The sections below give the equivalent commands, which are what the buttons run.

---

## 0. Prerequisites

| | |
|---|---|
| Host | Windows Server 2022 (MT5 is Windows-only). 2 vCPU / 4 GB is ample. |
| Location | Near the broker's server — London/LD4 for most gold brokers. |
| Python | 3.11 or 3.12 |
| Services | PostgreSQL 16 + TimescaleDB, Redis 7 (both via Docker Desktop or native) |
| MT5 | Terminal installed, logged in, **AutoTrading enabled** |

```powershell
git clone <repo> C:\xauusd && cd C:\xauusd
pip install uv
uv venv --python 3.11
uv pip install -e ".[dev,ml,api,db,mt5]"
copy .env.example .env      # then fill it in
```

Bring up the datastores and the schema:

```powershell
docker compose up -d          # postgres (+timescale) and redis, both bound to 127.0.0.1
alembic upgrade head
```

`POSTGRES_PASSWORD` has no default in `docker-compose.yml`; compose refuses to start
until `.env` sets it. Put the same password in `XAUUSD_DATABASE__URL` — `demo.yaml` and
`live.yaml` ship a placeholder (`xauusd:xauusd`) that you must not keep.

Configuration precedence, highest first:

    explicit arguments  >  environment  >  .env  >  config/*.yaml  >  defaults

So a variable in `.env` overrides the YAML, which is what makes the credentials above
work without editing a tracked file. This was the other way round until recently and
failed silently — see finding 19 in `docs/FINDINGS.md`.

---

## 1. Verify before anything else

```powershell
python -m xauusd.cli doctor --env demo
```

This must print `READY`. It checks config validity, database connectivity, broker
connectivity, and — critically — prints the **actual symbol specification** the broker
returns. Read those numbers. If `contract_size` is not 100 or `tick_value_loss` is not
what you expect for gold, stop and find out why before going further; every position
size in the system derives from them.

---

## 2. Start the bridge (Windows, alongside the terminal)

```powershell
python -m xauusd.cli bridge --host 127.0.0.1 --port 50551 `
    --terminal-path "C:\Program Files\MetaTrader 5\terminal64.exe"
```

Install as a service so it survives a reboot:

```powershell
nssm install xauusd-bridge "C:\xauusd\.venv\Scripts\python.exe" `
    "-m xauusd.cli bridge"
nssm set xauusd-bridge AppDirectory C:\xauusd
nssm set xauusd-bridge AppStdout C:\xauusd\logs\bridge.out.log
nssm start xauusd-bridge
```

The bridge binds to localhost by default. **Do not expose port 50551.** For remote
access use WireGuard, never a public port.

---

## 2b. The validation gate — before any of the stages below

Nothing reaches a live account until a strategy passes:

```powershell
python -m xauusd.cli validate --synthetic 60000
```

This is the Phase 10 deployment gate. It runs in-sample, out-of-sample, walk-forward,
Monte Carlo and regime splits with realistic spread, slippage and commission, and prints
a 24-criterion breakdown. **Expect it to fail.** A strategy that fails is not a broken
build; it is the gate doing the only job it has.

Until it passes, the strategy sits at `DEV` and the live-eligibility gate in
`strategy/gates.py` refuses to route it in `LIVE` mode. Paper and demo run regardless —
that is the point of them.

At the time of writing **no strategy has passed**, so live routing is unreachable today
no matter how the config is set. If you find yourself editing thresholds to get a pass,
read `docs/FINDINGS.md` first: the thresholds are the product, and a variant that
suddenly passes after a parameter change deserves more suspicion, not less.

---

## 3. Stage 4 — Paper trading

Live market data, simulated fills. Runs for at least two weeks.

```powershell
$env:XAUUSD_ENV="demo"
python -m xauusd.cli run
python -m xauusd.cli dashboard      # separate terminal
```

### Reaching the dashboard

It binds to `127.0.0.1:8000`. On the VPS itself, that is all you need.

To reach it from your own machine, **tunnel — do not bind it publicly**:

```powershell
ssh -N -L 8000:127.0.0.1:8000 you@your-vps      # then open http://localhost:8000
```

The dashboard can trip the kill switch and close every open position, so binding it to
a routable address without a token is refused at startup rather than served. If you have
a genuine reason to expose it (a WireGuard address, say), set a token first:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:XAUUSD_DASHBOARD__AUTH_TOKEN="<the generated token>"
$env:XAUUSD_DASHBOARD__HOST="10.8.0.2"          # the WireGuard address, never 0.0.0.0
```

The page then prompts for the token once and keeps it in that browser. Every `/api` path
requires it, including the read endpoints — the decision journal is the record of a live
trading account — and including the WebSocket, which takes it as a query parameter.

**HALT and FLATTEN** are recorded in `operator_commands` and executed by the engine on
its next poll (5s by default), never by the dashboard process. `FLATTEN` halts first, so
the engine cannot re-enter on the next M5 close. If any position fails to close you get
a CRITICAL alert and the command is recorded `FAILED` — it will never tell you the
account is flat when it is not. `GET /api/commands` is the audit trail.

**What you are looking for is not profit.** At this stage you are checking that the
machinery is sane:

- Open the **Rejection Ledger** daily. A healthy distribution is dominated by market
  conditions (`has_candidate`, `session`, `news_blackout`). If one technical gate
  dominates, that gate is misconfigured — this is the single most common finding here.
- Confirm the system is idle most of the time. Frequent trading is a symptom.
- Confirm the decision journal is filling: `python -m xauusd.cli rejections --hours 24`.
- Pick any decision and check the reasoning reads sensibly:
  `python -m xauusd.cli explain <id>`.

**Exit criteria:** two weeks with no crashes, no unexplained gaps in the journal, and a
rejection distribution you can explain.

---

## 4. Stage 5 — MT5 demo account

Same code, real order flow, no money.

```yaml
# config/demo.yaml
mode: DEMO
broker: { kind: mt5_grpc }
```

Now you are testing **execution**, not strategy:

- Every fill's slippage is recorded. After ~30 fills, refit the slippage distribution
  and **re-run validation with the measured costs**, not the assumed ones. This is the
  step most systems skip, and it is where a marginal edge usually disappears.
- Run the operational drills deliberately:
  - reboot the VPS mid-session — does the engine reconcile correctly on restart?
  - stop the bridge for 60 seconds — does the kill switch trip and clear?
  - let a position run through a Friday close — does weekend flattening fire?
- Reconcile the terminal's own history against the `positions` table by hand once.

**Exit criteria:** ≥30 demo trades, realised expectancy inside the Monte Carlo band from
the OOS distribution, zero execution defects, zero unexplained reconciliation
differences, and validation re-run with measured slippage.

---

## 5. Stage 6 — Small live account

### Arming (two keys, both required)

Key 1 — configuration:

```yaml
# config/live.yaml
mode: LIVE
live_trading: true
risk:
  global_risk_cap_pct: 0.0025    # 0.25%, far below the class caps
```

Key 2 — the arming file, which must match the connected account:

```powershell
python -m xauusd.cli arm-live 12345678
```

It asks for the account number twice and an explicit risk acknowledgement, then writes
`config/live_arming.json` (gitignored, machine-specific). A config edit alone cannot
arm live trading, and an arming file copied to another machine will not match.

Verify, then start:

```powershell
python -m xauusd.cli doctor --env live      # must print "live arming: ARMED"
python -m xauusd.cli run --env live --i-understand-this-is-live
```

### First live week

- Use money you can lose. The account size should make a total loss annoying, not
  material.
- Check the dashboard twice daily.
- Compare every live trade against what the backtest would have done at that timestamp.
- Do not change parameters. A week is far too little data to justify a change, and
  changing them resets whatever confidence the validation gave you.

---

## 6. Stage 7 — Scaling

Scaling steps are **pre-defined and mechanical**, never discretionary after a good week.
The cap is enforced in code (`risk.global_risk_cap_pct`), not by discipline.

| Step | Cap | Pre-condition |
|---|---|---|
| 1 | 0.25% | 20 live trades, no operational incidents |
| 2 | 0.50% | 50 live trades, realised expectancy within the OOS band |
| 3 | 0.75% | 100 live trades, max drawdown within the validated envelope |
| 4 | 1.00% (A) / 2.00% (A+) | 200 live trades, a full re-validation passed on live data |

Any of these reverts the cap one step immediately:

- realised expectancy outside the OOS Monte Carlo band over 30 trades
- model health reports `DEGRADED`
- a drawdown exceeding the validated maximum
- any unexplained reconciliation divergence

---

## 7. Daily operations

| When | Do |
|---|---|
| Each morning | Dashboard command centre; confirm kill switch clear and drawdown budgets |
| Each morning | Rejection ledger — has the distribution shifted? |
| Weekly | `python -m xauusd.cli rejections --hours 168`; review closed trades against their journalled reasoning |
| Weekly | Confirm `pg_dump` backups are running and restorable |
| Monthly | Re-run validation on all data including live; review model calibration drift |
| Quarterly | Update the curated calendar fallback in `economic_calendar.py` with next quarter's FOMC dates |

---

## 8. Incident response

**Kill switch tripped.** The dashboard names the reason. Auto-clearable conditions
(broker unreachable, stale data, wide spread, extreme news, daily drawdown) clear
themselves when the condition resolves. Weekly and monthly drawdown, state divergence
and spec change require a human:

```powershell
python -m xauusd.cli explain <decision_id>     # what the engine saw
# then, after understanding the cause, clear from the dashboard
```

**State divergence.** The engine and broker disagree about positions. **Do not restart
and hope.** Open the terminal, compare positions by hand against the `positions` table,
and only clear once you know which is right. This condition exists precisely because
guessing here is how accounts get double-exposed.

**Symbol spec changed.** The broker altered the contract. Every open position's risk
calculation is now invalid. Flatten manually, verify the new spec with `doctor`, then
restart.

**Engine crashed.** The supervisor restarts it. On boot it reconciles before trading —
check the logs for `startup_reconciliation` and confirm the resolution matches the
terminal. Server-side stops mean positions were protected throughout.

---

## 9. What "not deployable" looks like

It is a normal and expected outcome for the validation gate to fail and for nothing to
reach live. If that happens:

- The system still runs as an analysis and alerting tool. The rejection ledger and the
  decision journal are useful on their own.
- Resist the temptation to lower the thresholds. The thresholds are the product.
- The productive direction is almost always **more selectivity**, not more signals:
  narrow the session window, require a higher sweep quality, demand HTF alignment
  rather than mere non-conflict.
- A variant that suddenly passes after a parameter change deserves more suspicion than
  one that fails, not less.
