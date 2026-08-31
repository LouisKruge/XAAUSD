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
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from xauusd.config.settings import Settings, load_settings, verify_live_arming
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Mode
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
        spec = broker.symbol_spec(settings.symbol)
        print(
            f"symbol spec      : contract={spec.contract_size} "
            f"tick_value_loss={spec.tick_value_loss} step={spec.volume_step} "
            f"stops_level={spec.stops_level}"
        )
        if settings.mode is Mode.LIVE:
            armed, why = verify_live_arming(settings, account.login)
            print(f"live arming      : {'ARMED' if armed else 'NOT ARMED'} — {why}")
            ok = ok and armed
    except Exception as exc:
        print(f"broker           : FAILED — {exc}")
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
    serve(
        host=args.host,
        port=args.port,
        terminal_path=args.terminal_path,
        login=args.login,
        password=args.password,
        server=args.server,
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
            f"only {len(bars)} M5 bars in the database for {settings.symbol}. "
            f"Harvest history first, or pass --synthetic N for a smoke run."
        )
    m5 = BarSeries.from_bars(Timeframe.M5, bars)
    return build_timeframes(
        m5, [Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1]
    )


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
