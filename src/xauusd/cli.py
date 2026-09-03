"""Command-line entry points.

xauusd doctor              check configuration, database and broker connectivity
xauusd run                 start the live/demo/paper trading engine
xauusd dashboard           start the dashboard server
xauusd bridge              start the MT5 bridge (Windows only)
xauusd backtest            run a backtest over stored or generated data
xauusd validate            run the full validation suite and print the gate report
xauusd explain <id>        print the full reasoning for one decision
xauusd rejections          print the rejection ledger
xauusd arm-live            interactively create the live arming file (key 2 of 2)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from xauusd.config.settings import Settings, load_settings, verify_live_arming
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Direction, Mode
from xauusd.execution.symbol_discovery import (
    SymbolResolutionError,
    resolve_broker_symbol,
    resolve_symbol,
    sanity_check_quote,
)
from xauusd.monitoring.alerts import Notifier
from xauusd.monitoring.logging import configure_logging, get_logger

log = get_logger(__name__)


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()[:12]
    except Exception:
        return ""


def build_broker(settings: Settings):  # type: ignore[no-untyped-def]
    kind = settings.broker.kind
    if kind in ("mt5_grpc", "mt5_direct"):
        from xauusd.execution.mt5_broker import Mt5Broker

        return Mt5Broker(settings.broker.bridge_address, settings.broker.magic)
    if kind in ("sim", "paper"):
        from xauusd.domain.types import SymbolSpec
        from xauusd.execution.sim_broker import SimBroker

        spec = SymbolSpec(
            settings.symbol,
            2,
            0.01,
            100.0,
            0.01,
            1.0,
            1.0,
            1.0,
            0.01,
            50.0,
            0.01,
            10,
            5,
            commission_per_lot=settings.risk.commission_per_lot,
        )
        return SimBroker(spec, 10_000.0)
    raise ValueError(f"unknown broker kind {kind}")


# --------------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Pre-flight check. Run this before anything else on a new machine."""
    settings = load_settings(env=args.env)
    # WHERE this is running from, first. Two installations on one machine — an
    # unzipped copy and a re-downloaded one — is easy to end up with and impossible to
    # spot from a report that never says which folder produced it. That confusion
    # invalidates every other line here.
    from xauusd import __version__ as app_version

    print(f"install          : {Path.cwd()}")
    print(f"version          : {app_version}")
    print(
        f"config           : env={settings.env} mode={settings.mode} hash={settings.config_hash()}"
    )
    print(f"live_trading     : {settings.live_trading}")
    print(f"symbol           : {settings.symbol}")
    print(f"strategies       : {', '.join(settings.enabled_strategies)}")
    print(
        f"risk A / A+      : {settings.risk.risk_pct_a:.2%} / "
        f"{settings.risk.risk_pct_a_plus:.2%}  (global cap "
        f"{settings.risk.global_risk_cap_pct:.2%})"
    )
    print(
        f"drawdown d/w/m   : {settings.risk.max_daily_drawdown_pct:.1%} / "
        f"{settings.risk.max_weekly_drawdown_pct:.1%} / "
        f"{settings.risk.max_monthly_drawdown_pct:.1%}"
    )
    print(f"min RR           : {settings.thresholds.min_rr}")

    # Say plainly whether .env was found and used. Its absence was the failure that
    # produced a confident, entirely wrong report: correct credentials in the file,
    # a simulated broker in the output, and nothing connecting the two.
    env_path = Path(".env").resolve()
    if env_path.exists():
        from xauusd.config.bootstrap import parse_env

        keys = parse_env(env_path.read_text(encoding="utf-8", errors="replace"))
        filled = sum(1 for k, v in keys.items() if k.startswith("XAUUSD_") and v)
        print(f".env             : read from {env_path} ({filled} setting(s))")
    else:
        print(f".env             : NOT FOUND at {env_path} — settings come from config/ only")

    dash = settings.dashboard
    if dash.is_loopback:
        exposure = f"loopback ({dash.host}:{dash.port}) — tunnel to reach it remotely"
    elif dash.auth_token:
        exposure = f"{dash.host}:{dash.port} WITH a token — reachable off-host"
    else:  # DashboardConfig refuses to construct in this state; belt and braces.
        exposure = f"{dash.host}:{dash.port} WITHOUT a token — REFUSED at startup"
    print(f"dashboard        : {exposure}")

    ok = True
    try:
        db = Database(settings.database.url)
        db.create_all()
        with db.session() as s:
            n = len(Repositories(s).strategy_status.all())
        print(f"database         : OK ({settings.database.url}) — {n} strategy records")
    except Exception as exc:
        print(f"database         : FAILED — {exc}")
        ok = False

    try:
        broker = build_broker(settings)
        health = broker.health()
        account = broker.account()
        print(
            f"broker           : {'OK' if health.is_ok else 'DEGRADED'} "
            f"(kind={settings.broker.kind}) login={account.login} "
            f"equity={account.equity:.2f} {account.currency}"
        )
        if not health.is_ok:
            # Name the failing flag. "DEGRADED" alone sends an operator hunting through
            # three unrelated causes, only one of which is ever the real one.
            checks = (
                ("connected", health.connected, "the terminal is not connected to the broker"),
                (
                    "trade_allowed",
                    health.trade_allowed,
                    "the account may not trade (investor password, or the market is closed)",
                ),
                (
                    "trade_expert",
                    health.trade_expert,
                    "AutoTrading is OFF — press the AutoTrading button in MT5",
                ),
            )
            for name, value, fix in checks:
                if not value:
                    print(f"                   FAILING: {name} — {fix}")
            if health.detail:
                print(f"                   detail: {health.detail}")

        # Resolve the symbol exactly as the engine does, rather than trusting the
        # configured name. Brokers call gold XAUUSD, XAUUSD.a, XAUUSDm, GOLD, GOLD.spot
        # and more; a pre-flight that checks a name the engine would never use is
        # checking the wrong instrument, and fails on brokers where the engine works.
        symbol = settings.symbol
        raw = getattr(broker, "raw_symbols", None)
        if raw is not None:
            try:
                chosen = resolve_symbol(
                    raw(settings.data.symbol_patterns, settings.data.symbol_override),
                    settings.data.symbol_patterns,
                    settings.data.symbol_override,
                )
                symbol = chosen.name
                note = "" if symbol == settings.symbol else f"  (config says {settings.symbol})"
                print(f"symbol resolved  : {symbol}{note} — {chosen.description}")
            except Exception as exc:
                print(f"symbol resolved  : FAILED — {exc}")
                print(
                    "                   the broker offers no tradable USD gold symbol "
                    "matching the configured patterns. Check the exact name in MT5's "
                    "Market Watch and set XAUUSD_DATA__SYMBOL_OVERRIDE in .env."
                )
                ok = False

        spec = broker.symbol_spec(symbol)
        print(
            f"symbol spec      : contract={spec.contract_size} digits={spec.digits} "
            f"tick_size={spec.tick_size} tick_value_loss={spec.tick_value_loss}"
        )
        print(
            f"                   volume {spec.volume_min}-{spec.volume_max} "
            f"step {spec.volume_step} | stops_level={spec.stops_level}"
        )
        # The one arithmetic check an operator cannot reasonably do by eye, and the one
        # that decides every position size: a one-tick move on one lot is worth
        # contract_size * tick_size in the profit currency. If the broker's own numbers
        # disagree with that, sizing derived from them cannot be trusted.
        implied = spec.contract_size * spec.tick_size
        mismatch = spec.tick_value_loss > 0 and abs(implied - spec.tick_value_loss) > 0.01 * implied

        # Ask the broker to settle it. OrderCalcProfit is what the terminal would
        # actually credit or debit, which outranks any descriptive field: a spec can be
        # filled in wrongly, but this is the arithmetic the money follows.
        # The price is the only thing that proves the instrument is spot gold. A symbol
        # can be named GOLD, described "SPOT Gold Ounce vs US Dollar", and carry an ISIN,
        # NYSE listing and "Basic Materials" sector — because it is a mining company's
        # shares. Its contract size, tick size and tick value would all still look
        # perfectly reasonable. The quote is what separates them. The engine checks this
        # at startup; a pre-flight that does not is checking a different thing again.
        # Only for a real broker: SimBroker has no market data until a backtest loads
        # some, so it quotes 0.0, and failing the pre-flight on that would be reporting
        # the simulator's emptiness as a broker fault.
        try:
            q = broker.quote(symbol)
            print(f"quote            : bid {q.bid} / ask {q.ask}")
            if settings.broker.kind.startswith("mt5"):
                sanity_check_quote(q.mid, symbol)
        except SymbolResolutionError as exc:
            print(f"                   NOT GOLD: {exc}")
            print(
                "                   A symbol can be named GOLD and be the mining "
                "company's stock. Check the price against spot gold in MT5 and set "
                "XAUUSD_DATA__SYMBOL_OVERRIDE to the right instrument."
            )
            ok = False
        except Exception as exc:
            print(f"quote            : unavailable — {exc}")

        measured: float | None = None
        try:
            quote = broker.quote(symbol)
            measured = broker.calc_profit(
                symbol, Direction.LONG, 1.0, quote.ask, quote.ask + spec.tick_size
            )
        except Exception as exc:
            print(f"                   (broker could not price a test tick: {exc})")

        if mismatch:
            print(
                f"                   MISMATCH: contract x tick_size = {implied:.4f} "
                f"but the broker reports tick_value_loss={spec.tick_value_loss} "
                f"({implied / spec.tick_value_loss:.0f}x apart)"
            )
            if measured is not None:
                truth = (
                    "contract x tick_size"
                    if abs(measured - implied) < abs(measured - spec.tick_value_loss)
                    else "tick_value_loss"
                )
                print(
                    f"                   broker prices a 1-tick move on 1 lot at "
                    f"{measured:.4f} {spec.currency_profit} — which matches {truth}"
                )
            print(
                "                   Sizing uses tick_value_loss, so every position would "
                "be wrong by that factor. Do not trade this symbol until it is explained; "
                "a different broker's demo is usually the fastest answer."
            )
            ok = False
        else:
            print(
                f"                   consistent: 1 tick on 1 lot = "
                f"{spec.contract_size} x {spec.tick_size} = {implied:.2f} "
                f"{spec.currency_profit}"
                + (f" (broker prices it at {measured:.2f})" if measured is not None else "")
            )
        if settings.mode is Mode.LIVE:
            armed, why = verify_live_arming(settings, account.login)
            print(f"live arming      : {'ARMED' if armed else 'NOT ARMED'} — {why}")
            ok = ok and armed
    except Exception as exc:
        print(f"broker           : FAILED — {exc}")
        # "unreachable" has two very different causes with two different fixes, and the
        # socket error alone does not distinguish them. Ask the port directly.
        if settings.broker.kind.startswith("mt5"):
            host, _, port_s = settings.broker.bridge_address.partition(":")
            port = int(port_s or 50551)
            listening = False
            try:
                with socket.create_connection((host, port), timeout=3):
                    listening = True
            except OSError:
                listening = False

            print(f"                   bridge address: {host}:{port}")
            if listening:
                print(
                    "                   something IS listening there but did not answer — "
                    "the bridge is running yet stuck, most often because the MT5 terminal "
                    "was closed underneath it. Stop the bot, make sure MT5 is open and "
                    "logged in, and start it again."
                )
            else:
                print(
                    "                   nothing is listening there — the bridge process is "
                    "not running. Use Stop XAUUSD Bot, then Start XAUUSD Bot."
                )
            log = Path("logs/bridge.log")
            if log.exists():
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
                if tail:
                    print("                   last lines of logs/bridge.log:")
                    for line in tail:
                        print(f"                     {line[:150]}")
            else:
                print(
                    "                   logs/bridge.log does not exist, so the bridge has "
                    "never started in this installation."
                )
        ok = False

    print()
    print("READY" if ok else "NOT READY — fix the failures above before running")
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(env=args.env)
    configure_logging(settings.log_level, settings.log_json, "logs/engine.log")
    if settings.mode is Mode.LIVE and not args.i_understand_this_is_live:
        print("Refusing to start in LIVE mode without --i-understand-this-is-live.")
        print("Run 'xauusd doctor' first and confirm the arming file matches the account.")
        return 2
    db = Database(settings.database.url)
    db.create_all()
    broker = build_broker(settings)

    from xauusd.engine.orchestrator import TradingEngine

    engine = TradingEngine(settings, broker, db, Notifier(settings.alerts), git_sha())
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        engine.stop()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    configure_logging("INFO", json_output=False)
    from xauusd.dashboard.api import run

    run(host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from xauusd.execution.mt5_bridge import serve

    configure_logging("INFO", json_output=False, log_file="logs/bridge.log")

    # Fall back to the configured broker for anything not given on the command line.
    # Without this the four XAUUSD_BROKER__* values that .env.example asks for were read
    # by nothing: the bridge is launched with no flags, so it silently ignored the
    # credentials and worked only because MT5 happened to be logged in already.
    b = load_settings(env=args.env).broker
    login = args.login if args.login is not None else b.login
    password = args.password if args.password is not None else b.password
    server = args.server if args.server is not None else b.server
    terminal_path = args.terminal_path if args.terminal_path is not None else b.terminal_path

    if login:
        log.info("bridge_credentials", source="config", login=login, server=server or "(default)")
    else:
        # Not an error: mt5.initialize() attaches to whatever session the terminal has
        # open. Worth saying out loud, because "connected to the wrong account" is a
        # much worse discovery later.
        log.warning(
            "bridge_no_login_configured",
            detail="attaching to the account already open in the MT5 terminal",
        )

    serve(
        host=args.host,
        port=args.port,
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    configure_logging("WARNING", json_output=False)
    settings = load_settings(env=args.env)

    from xauusd.backtesting.engine import BacktestConfig, BacktestEngine
    from xauusd.domain.types import SymbolSpec

    data = _load_data(args, settings)
    spec = SymbolSpec(
        settings.symbol,
        2,
        0.01,
        100.0,
        0.01,
        1.0,
        1.0,
        1.0,
        0.01,
        50.0,
        0.01,
        10,
        5,
        commission_per_lot=settings.risk.commission_per_lot,
    )
    engine = BacktestEngine(
        settings,
        spec,
        BacktestConfig(
            starting_equity=args.equity,
            warmup_bars=args.warmup,
            step=args.step,
            progress_every=args.progress,
        ),
    )
    result = engine.run(data)
    print(result.summary())
    print()
    print("Rejection ledger (why it did not trade):")
    for gate, n in list(result.rejection_ledger.items())[:12]:
        print(f"  {gate:<30} {n}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "metrics": result.metrics.as_dict(),
                    "rejections": result.rejection_ledger,
                    "period": [result.period_start.isoformat(), result.period_end.isoformat()],
                    "cost_model": result.cost_model,
                    "data_hash": result.data_hash,
                    "config_hash": result.config_hash,
                },
                indent=2,
                default=str,
            )
        )
        print(f"\nwrote {args.json}")
    return 0


def _load_data(args: argparse.Namespace, settings: Settings) -> dict:
    """Load bars from the database, or generate synthetic data for a smoke run."""
    from xauusd.data.resample import build_timeframes
    from xauusd.data.series import BarSeries
    from xauusd.domain.enums import Timeframe

    if args.synthetic:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tests.fixtures.synthetic import market

        print(
            f"using SYNTHETIC data ({args.synthetic} M5 bars) — for smoke testing only; "
            f"results are meaningless as a trading result"
        )
        return market(args.synthetic, seed=args.seed)

    db = Database(settings.database.url)
    with db.session() as s:
        repos = Repositories(s)
        bars = repos.bars.load(settings.symbol, Timeframe.M5, source=args.source)
    if len(bars) < 5000:
        raise SystemExit(
            f"only {len(bars)} M5 bars in the database for {settings.symbol}.\n"
            f"Download history first: dashboard System tab -> 'Download price "
            f"history', or `xauusd harvest`. The MT5 bridge must be running.\n"
            f"`--synthetic N` runs a smoke test instead, but generated data has no "
            f"genuine market structure, so it correctly produces no trades."
        )
    m5 = BarSeries.from_bars(Timeframe.M5, bars)
    return build_timeframes(
        m5, [Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1]
    )


def cmd_harvest(args: argparse.Namespace) -> int:
    """Pull real bar history from the broker into the database.

    Without this the only runnable backtest is `--synthetic`, and the pipeline suite
    asserts synthetic data produces no trades. An operator reading "0 trades" then has
    no way to tell a selective strategy from an empty database.
    """
    configure_logging("WARNING", json_output=False)
    settings = load_settings(env=args.env)

    from xauusd.data.harvest import coverage, harvest
    from xauusd.domain.enums import Timeframe

    db = Database(settings.database.url)
    broker = build_broker(settings)
    symbol = resolve_broker_symbol(broker, settings)
    held, first, last = coverage(db, symbol, Timeframe.M5, source=args.source)
    print(f"symbol           : {symbol}")
    print(
        f"already held     : {held} M5 bars"
        + (f"  ({first:%Y-%m-%d} -> {last:%Y-%m-%d})" if held else "")
    )
    print(f"requesting       : {args.bars} bars, newest first\n")

    def show(done: int, want: int) -> None:
        print(f"   {done:>7,} / {want:,}", end="\r", flush=True)

    report = harvest(
        broker,
        db,
        symbol,
        Timeframe.M5,
        wanted=args.bars,
        source=args.source,
        progress=show,
    )

    print(" " * 40, end="\r")
    print(report.summary())

    held, _, _ = coverage(db, symbol, Timeframe.M5, source=args.source)
    print(f"\nheld now         : {held} M5 bars")
    if held < 5000:
        print(
            "                   that is below the 5000 a backtest needs; run this "
            "again when the terminal has more history loaded."
        )
        return 1
    print("                   enough to backtest. Run a backtest without --synthetic.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """The Phase 10 deployment gate — the only thing that can make a strategy live-eligible.

    Delegates to scripts/run_validation.py so there is exactly one implementation of the
    gate. Expect it to FAIL for most strategy versions; that is the gate working.
    """
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "run_validation.py"
    if not script.exists():
        print(f"validation script not found at {script}", file=sys.stderr)
        return 2

    cmd = [sys.executable, str(script)]
    for flag, value in (
        ("--synthetic", args.synthetic),
        ("--seed", args.seed),
        ("--source", args.source),
        ("--warmup", args.warmup),
        ("--step", args.step),
        ("--strategy", args.strategy),
        ("--json", args.json),
    ):
        if value is not None:
            cmd += [flag, str(value)]
    return subprocess.call(cmd)


def cmd_explain(args: argparse.Namespace) -> int:
    settings = load_settings(env=args.env)
    db = Database(settings.database.url)
    with db.session() as s:
        row = Repositories(s).decisions.get(args.decision_id)
        if row is None:
            print(f"decision {args.decision_id} not found")
            return 1
        print(f"DECISION {row.id} @ {row.ts}  [{row.mode}]")
        print(f"  Classification : {row.classification}")
        print(f"  Strategy       : {row.strategy} v{row.strategy_version}")
        print(f"  Direction      : {row.direction}")
        if row.planned_entry:
            print(
                f"  Entry/SL/TP    : {row.planned_entry} / {row.planned_sl} / "
                f"{row.planned_tp2 or row.planned_tp1}  RR {row.planned_rr}"
            )
        print(f"  Score          : {row.setup_score}")
        print(f"  Probability    : {row.probability}")
        if row.score_breakdown:
            print("  Score breakdown:")
            for k, v in (row.score_breakdown.get("categories") or {}).items():
                mx = (row.score_breakdown.get("maximums") or {}).get(k, 0)
                print(f"      {k:<26} {v:5.1f} / {mx}")
            for k, v in (row.score_breakdown.get("penalties") or {}).items():
                print(f"      PENALTY {k:<18} -{v}")
        failed = [g for g in (row.gate_trace or []) if not g.get("passed")]
        if failed:
            print("  BLOCKED BY:")
            for g in failed:
                print(
                    f"      {g['gate']:<26} observed={g.get('observed')!r} "
                    f"required={g.get('threshold')!r}"
                )
        else:
            print("  All gates passed.")
        for label, items in (
            ("Reasons for", row.reasons_for),
            ("Reasons against", row.reasons_against),
        ):
            if items:
                print(f"  {label}:")
                for r in items:
                    print(f"      - {r}")
        print(f"  Invalidation   : {row.invalidation}")
        print(f"  config={row.config_hash} git={row.git_sha} latency={row.latency_ms}ms")
    return 0


def cmd_rejections(args: argparse.Namespace) -> int:
    settings = load_settings(env=args.env)
    db = Database(settings.database.url)
    end = datetime.now(UTC)
    start = end - timedelta(hours=args.hours)
    with db.session() as s:
        repos = Repositories(s)
        counts = repos.decisions.counts_by_classification(start, end)
        ledger = repos.decisions.rejection_ledger(start, end)
    total = sum(counts.values())
    print(f"Last {args.hours}h: {total} evaluations")
    for k, v in sorted(counts.items()):
        print(f"  {k:<12} {v}")
    if total:
        traded = total - counts.get("NO_TRADE", 0)
        print(f"  selectivity  {traded / total:.4%}")
    print("\nWhy it did not trade:")
    for gate, n in ledger:
        print(f"  {gate:<30} {n:>6}  {n / total:.1%}" if total else f"  {gate:<30} {n}")
    return 0


def cmd_arm_live(args: argparse.Namespace) -> int:
    """Create the arming file. This is key 2 of 2; the config flag is key 1."""
    settings = load_settings(env=args.env)
    path = Path(settings.live_arming_file)
    print("LIVE ARMING")
    print("This creates the second of two keys required for live trading.")
    print("The first key is 'live_trading: true' with 'mode: LIVE' in your config.")
    print()
    print(f"Account number to arm: {args.account}")
    print(f"Global risk cap      : {settings.risk.global_risk_cap_pct:.3%} per trade")
    print()
    confirm = input("Type the account number again to confirm: ").strip()
    if confirm != str(args.account):
        print("Mismatch. Not armed.")
        return 1
    ack = input("Type 'I ACCEPT THE RISK' to continue: ").strip()
    if ack != "I ACCEPT THE RISK":
        print("Not armed.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "account_login": int(args.account),
                "acknowledged_risk": True,
                "armed_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
    )
    print(f"\nWrote {path}. This file is gitignored and machine-specific.")
    print("Run 'xauusd doctor' to verify before starting the engine.")
    return 0


# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="xauusd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--env", default=None, help="config environment (dev/demo/live)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, database and broker").set_defaults(func=cmd_doctor)

    run_p = sub.add_parser("run", help="start the trading engine")
    run_p.add_argument("--i-understand-this-is-live", action="store_true")
    run_p.set_defaults(func=cmd_run)

    dash = sub.add_parser("dashboard", help="start the dashboard server")
    dash.add_argument("--host", default=None, help="overrides dashboard.host")
    dash.add_argument("--port", type=int, default=None, help="overrides dashboard.port")
    dash.add_argument("--reload", action="store_true")
    dash.set_defaults(func=cmd_dashboard)

    br = sub.add_parser("bridge", help="start the MT5 bridge (Windows only)")
    br.add_argument("--host", default="127.0.0.1")
    br.add_argument("--port", type=int, default=50551)
    br.add_argument("--terminal-path", default=None)
    br.add_argument("--login", type=int, default=None)
    br.add_argument("--password", default=None)
    br.add_argument("--server", default=None)
    br.set_defaults(func=cmd_bridge)

    bt = sub.add_parser("backtest", help="run a backtest")
    bt.add_argument(
        "--synthetic",
        type=int,
        default=0,
        help="generate N synthetic M5 bars instead of loading history",
    )
    bt.add_argument("--seed", type=int, default=5)
    bt.add_argument("--source", default="mt5")
    bt.add_argument("--equity", type=float, default=10_000.0)
    bt.add_argument("--warmup", type=int, default=6000)
    bt.add_argument("--step", type=int, default=3)
    bt.add_argument("--progress", type=int, default=5000)
    bt.add_argument("--json", default=None, help="write metrics to this path")
    bt.set_defaults(func=cmd_backtest)

    hv = sub.add_parser("harvest", help="download real bar history from the broker")
    hv.add_argument("--bars", type=int, default=60_000, help="how many M5 bars to hold")
    hv.add_argument("--source", default="mt5")
    hv.set_defaults(func=cmd_harvest)

    va = sub.add_parser("validate", help="run the full validation suite (the deployment gate)")
    va.add_argument("--synthetic", type=int, default=None, help="generate N synthetic M1 bars")
    va.add_argument("--seed", type=int, default=None)
    va.add_argument("--source", default=None, help="bar source when not synthetic")
    va.add_argument("--warmup", type=int, default=None)
    va.add_argument("--step", type=int, default=None)
    va.add_argument("--strategy", default=None)
    va.add_argument("--json", default=None, help="write the gate report to this path")
    va.set_defaults(func=cmd_validate)

    ex = sub.add_parser("explain", help="print the full reasoning for one decision")
    ex.add_argument("decision_id", type=int)
    ex.set_defaults(func=cmd_explain)

    rj = sub.add_parser("rejections", help="print the rejection ledger")
    rj.add_argument("--hours", type=int, default=24)
    rj.set_defaults(func=cmd_rejections)

    arm = sub.add_parser("arm-live", help="create the live arming file")
    arm.add_argument("account", type=int)
    arm.set_defaults(func=cmd_arm_live)

    args = p.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ValidationError as exc:
        # A bad config is the most common reason any of these commands cannot run, and
        # `doctor` in particular exists to say what is wrong. A pydantic traceback buries
        # that message under twenty frames of validator internals.
        print("configuration is invalid:\n", file=sys.stderr)
        for err in exc.errors():
            where = ".".join(str(x) for x in err["loc"]) or "(root)"
            print(f"  {where}: {err['msg']}", file=sys.stderr)
        print("\nNOT READY — fix the configuration above.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
