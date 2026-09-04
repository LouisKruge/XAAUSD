"""The live trading engine.

Loop structure, matching docs/architecture/00-overview.md:

    every 1s    tick monitor    — spread, staleness, kill-switch triggers, position mgmt
    on M5 close decision cycle  — the main heartbeat
    every 60s   reconciliation  — broker truth versus our record
    every 5m    context refresh — macro, calendar, news
    nightly     analytics rollup

Two things happen before the engine will trade at all:
  * a single-instance lock is acquired, because two engines against one account means
    duplicate positions and double risk;
  * every unresolved order from a previous run is reconciled against the broker.
    Never assume a pre-crash view of positions is still true.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from xauusd.config.settings import Settings, verify_live_arming
from xauusd.core.analyzer import snapshot_payload
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.core.sessions import BrokerClock, SessionEngine
from xauusd.data.marketview import MarketView
from xauusd.data.series import BarSeries
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import (
    Classification,
    KillSwitchReason,
    Mode,
    Timeframe,
    ValidationStatus,
)
from xauusd.domain.types import Decision, MacroState, NewsState, SymbolSpec
from xauusd.engine.continuous import ContinuousScanner, ScanOutcome
from xauusd.engine.pipeline import DecisionPipeline, EngineState, client_tag
from xauusd.engine.scalp_pipeline import ScalpPipeline
from xauusd.execution.broker import Broker, BrokerError
from xauusd.execution.order_manager import OrderManager
from xauusd.execution.position_manager import PositionManager
from xauusd.execution.reconciler import Reconciler
from xauusd.execution.symbol_discovery import resolve_symbol, sanity_check_quote
from xauusd.monitoring.alerts import Notifier
from xauusd.monitoring.health import HealthRegistry
from xauusd.monitoring.logging import cycle_context, get_logger
from xauusd.risk.correlation import OpenExposure
from xauusd.risk.drawdown import DrawdownGuard
from xauusd.risk.gate import RiskGate
from xauusd.risk.kill_switch import KillSwitch

log = get_logger(__name__)


class SingleInstanceLock:
    """Two engines against one account is catastrophic. Refuse to start without the lock.

    Postgres advisory lock in production; a file lock for SQLite/local runs. Either way
    the engine refuses to start rather than racing.
    """

    LOCK_KEY = 0x5841_5555  # "XAUU"

    def __init__(self, db: Database) -> None:
        self.db = db
        self._held = False
        self._file: Path | None = None

    def acquire(self) -> bool:
        if self.db.url.startswith("postgresql"):
            from sqlalchemy import text

            with self.db.session() as s:
                got = s.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": self.LOCK_KEY}
                ).scalar()
            self._held = bool(got)
            return self._held

        import atexit
        import os

        path = Path("data/engine.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip() or 0)
                os.kill(pid, 0)
                return False  # a live process holds it
            except (ValueError, ProcessLookupError, PermissionError):
                path.unlink(missing_ok=True)  # stale lock from a crash
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self._file = path
        self._held = True
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if self._file is not None:
            self._file.unlink(missing_ok=True)
            self._file = None
        self._held = False


@dataclass(slots=True)
class ContextCache:
    """Macro, calendar and news, refreshed on a slower schedule than decisions."""

    macro: MacroState | None = None
    news: NewsState | None = None
    refreshed_at: datetime | None = None
    ttl_seconds: float = 300.0

    def stale(self, now: datetime) -> bool:
        return (
            self.refreshed_at is None
            or (now - self.refreshed_at).total_seconds() > self.ttl_seconds
        )


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        db: Database,
        notifier: Notifier | None = None,
        git_sha: str = "",
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.db = db
        self.notifier = notifier or Notifier(settings.alerts)
        self.health = HealthRegistry()

        self.kill_switch = KillSwitch(self.notifier)
        self.drawdown = DrawdownGuard(settings.risk)
        self.risk_gate = RiskGate(settings, self.drawdown, self.kill_switch)
        self.pipeline = DecisionPipeline(settings, risk_gate=self.risk_gate, git_sha=git_sha)

        # The short-duration engine. It shares this risk gate deliberately: a scalp and
        # an A+ draw from the same drawdown budget, the same kill switch and the same
        # open-risk cap, so they must consult one instance rather than two that agree
        # by coincidence.
        self.micro = MicroAnalyzer(settings)
        self.scalp = ScalpPipeline(settings, risk_gate=self.risk_gate)
        self.scalp_scanner = ContinuousScanner(
            self._scalp_scan,
            interval_seconds=settings.scalp.scan_interval_seconds,
            name="scalp",
            should_run=lambda: (
                self.running
                and settings.scalp.enabled
                and self.sessions.is_market_open(datetime.now(UTC))
            ),
        )
        self.orders = OrderManager(
            broker, settings, self.kill_switch, self.notifier, persist=self._persist_order
        )
        self.positions = PositionManager(broker, settings)
        self.reconciler = Reconciler(broker, self.kill_switch, self.notifier, settings.broker.magic)
        self.clock = BrokerClock()
        self.sessions = SessionEngine(settings.session, self.clock)
        self.context = ContextCache()
        self.lock = SingleInstanceLock(db)

        self.symbol: str = settings.symbol
        self.spec: SymbolSpec | None = None
        self.spec_hash: str | None = None
        self.running = False
        self.trades_today = 0
        self._last_decision_bar: int | None = None
        self._day: Any = None

    # -- startup -----------------------------------------------------------------------

    def startup(self) -> bool:
        """Everything that must succeed before the engine is allowed to trade."""
        if not self.lock.acquire():
            log.error(
                "single_instance_lock_failed",
                detail="another engine already holds the lock; refusing to start",
            )
            self.notifier.critical(
                "STARTUP", "Refused to start", "another engine instance holds the lock"
            )
            return False

        try:
            account = self.broker.account()
        except BrokerError as exc:
            log.error("broker_unreachable_at_startup", error=str(exc))
            return False

        # A crash between claiming an operator command and executing it would strand it
        # at CLAIMED. Both commands are idempotent, so returning them to the queue is
        # safer than dropping what may be an emergency stop.
        with self.db.session() as s:
            stranded = Repositories(s).commands.requeue_stale_claims()
        if stranded:
            log.warning("operator_commands_requeued", ids=stranded)
            self.notifier.critical(
                "STARTUP",
                "Operator commands were interrupted",
                f"commands {stranded} were claimed but never completed; re-queued",
            )

        # Two-key arming. The config flag alone can never enable live trading.
        if self.settings.mode is Mode.LIVE:
            armed, why = verify_live_arming(self.settings, account.login)
            if not armed:
                log.error("live_arming_failed", reason=why)
                self.notifier.critical("STARTUP", "Live trading NOT armed", why)
                return False
            log.warning("live_trading_armed", account=account.login)
            self.notifier.critical(
                "STARTUP",
                "LIVE TRADING ARMED",
                f"account {account.login}, cap {self.settings.risk.global_risk_cap_pct:.2%}",
            )

        if not self._resolve_symbol():
            return False

        # Reconcile BEFORE trading. Never assume the pre-crash view is still true.
        with self.db.session() as s:
            repos = Repositories(s)
            unresolved = [o.client_tag for o in repos.orders.unresolved()]
            open_rows = [
                {
                    "mt5_position": p.mt5_position,
                    "strategy": p.strategy,
                    "current_sl": float(p.current_sl or 0),
                    "volume": float(p.volume),
                    "remaining_volume": float(p.remaining_volume),
                }
                for p in repos.positions.get_open()
            ]
            repos.config.record(
                self.settings.config_hash(), self.settings.as_dict(), self.pipeline.git_sha
            )
        if unresolved:
            log.warning("reconciling_unresolved_orders", count=len(unresolved))
            self.orders.reconcile_unresolved(unresolved, self.settings.broker.magic)

        result = self.reconciler.reconcile(open_rows)
        if not result.clean:
            log.warning("startup_reconcile", summary=result.summary())
        if result.critical:
            return False

        self.drawdown.update(datetime.now(UTC), account.equity)
        self.health.report("broker", True)
        self.health.report("database", True)
        log.info(
            "engine_ready",
            mode=str(self.settings.mode),
            symbol=self.symbol,
            equity=account.equity,
            config_hash=self.settings.config_hash(),
        )
        return True

    def _resolve_symbol(self) -> bool:
        raw = getattr(self.broker, "raw_symbols", None)
        try:
            if raw is not None:
                candidates = raw(
                    self.settings.data.symbol_patterns, self.settings.data.symbol_override
                )
                chosen = resolve_symbol(
                    candidates,
                    self.settings.data.symbol_patterns,
                    self.settings.data.symbol_override,
                )
                self.symbol = chosen.name
            self.spec = self.broker.symbol_spec(self.symbol)
            self.spec_hash = self.spec.spec_hash()
            quote = self.broker.quote(self.symbol)
            sanity_check_quote(quote.mid, self.symbol)
        except Exception as exc:
            log.error("symbol_resolution_failed", error=str(exc))
            self.notifier.critical("STARTUP", "Symbol resolution failed", str(exc))
            return False
        log.info(
            "symbol_ready",
            symbol=self.symbol,
            spec_hash=self.spec_hash,
            contract_size=self.spec.contract_size,
            tick_value=self.spec.tick_value_loss,
        )
        return True

    # -- main loop ---------------------------------------------------------------------

    async def run(self) -> None:
        if not self.startup():
            return
        self.running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows has no add_signal_handler; the engine still stops via stop().
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

        await asyncio.gather(
            self._tick_loop(),
            self._decision_loop(),
            # A separate task, not a step inside the M5 cycle: a slow scalp scan must
            # never delay the A/A+ decision, and vice versa.
            self.scalp_scanner.run(),
            self._reconcile_loop(),
            self._context_loop(),
            self._command_loop(),
            return_exceptions=True,
        )

    def stop(self) -> None:
        log.warning("engine_stopping")
        self.running = False

    async def _tick_loop(self) -> None:
        """1 Hz: freshness, spread, kill-switch conditions, position management."""
        while self.running:
            try:
                now = datetime.now(UTC)
                quote = self.broker.quote(self.symbol)
                self.clock.observe(quote.ts, now)
                health = self.broker.health()
                self.health.report("broker", health.is_ok, health.latency_ms)

                spread_pts = quote.spread_points(self.spec.point) if self.spec else 0.0
                self.kill_switch.evaluate(
                    now,
                    broker_ok=health.is_ok,
                    quote_age_seconds=(now - quote.ts).total_seconds(),
                    max_quote_age=self.settings.execution.max_quote_age_seconds,
                    spread_points=spread_pts,
                    spread_median=25.0,
                    max_spread_ratio=self.settings.regime.abnormal_spread_multiple,
                    market_open=self.sessions.is_market_open(now),
                )
                if self.spec:
                    self.positions.manage(quote, now, self.spec)
            except BrokerError as exc:
                self.health.report("broker", False)
                self.kill_switch.trip(
                    KillSwitchReason.BROKER_UNREACHABLE, str(exc), now=datetime.now(UTC)
                )
            except Exception as exc:
                log.error("tick_loop_error", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)

    async def _decision_loop(self) -> None:
        """Wakes on M5 close. Never evaluates mid-bar."""
        tf = Timeframe.M5
        while self.running:
            try:
                await self._sleep_to_next_close(tf)
                if not self.running:
                    break
                with cycle_context():
                    await asyncio.to_thread(self._decision_cycle)
            except Exception as exc:
                log.error("decision_loop_error", error=f"{type(exc).__name__}: {exc}")
                self.health.report("engine", False)
                await asyncio.sleep(5)

    async def _sleep_to_next_close(self, tf: Timeframe) -> None:
        now = datetime.now(UTC)
        epoch = int(now.timestamp())
        next_close = ((epoch // tf.seconds) + 1) * tf.seconds
        # A small guard delay so the broker has published the closing bar.
        await asyncio.sleep(max(0.5, next_close - epoch + 2.0))

    def _decision_cycle(self) -> None:
        now = datetime.now(UTC)
        t0 = time.perf_counter()

        day = self.clock.to_broker(now).date()
        if self._day != day:
            self._day, self.trades_today = day, 0

        try:
            account = self.broker.account()
            quote = self.broker.quote(self.symbol)
            spec = self.broker.symbol_spec(self.symbol)
        except BrokerError as exc:
            log.error("cycle_broker_error", error=str(exc))
            return

        if spec.spec_hash() != self.spec_hash:
            self.kill_switch.trip(
                KillSwitchReason.SPEC_CHANGED,
                f"symbol spec changed from {self.spec_hash} to {spec.spec_hash()}",
                now=now,
            )
            return
        self.spec = spec

        source = _BrokerBarSource(self.broker, self.settings)
        view = MarketView(source, self.symbol, now, quote)

        positions = self.broker.positions(magic=self.settings.broker.magic)
        state = EngineState(
            account=account,
            spec=spec,
            health=self.broker.health(),
            open_positions=positions,
            open_risk_pct=self._open_risk(positions, account.equity, spec),
            trades_today=self.trades_today,
            existing_tags={p.client_tag for p in positions if p.client_tag},
            strategy_status=self._strategy_status(),
            symbol_resolved=True,
            spec_unchanged=True,
            # Hand the sizer an independent second opinion on what a loss costs. Without
            # it PositionSizer's cross-check never runs, and a symbol spec whose tick
            # value disagrees with its contract size and tick size sizes every position
            # wrongly with nothing to catch it.
            calc_profit=lambda direction, entry, stop: self.broker.calc_profit(
                self.symbol, direction, 1.0, entry, stop
            ),
        )

        result = self.pipeline.run(
            view,
            state,
            macro=self.context.macro,
            news=self.context.news,
            spread_points=quote.spread_points(spec.point),
        )
        self._persist_cycle(result)

        if result.executable is not None:
            self._execute(result.executable, spec, now)

        self.health.report("engine", True, int((time.perf_counter() - t0) * 1000))

    def _scalp_scan(self) -> ScanOutcome:
        """One continuous-scan pass. Runs off the M5 cycle, on its own cadence.

        Everything is re-read here rather than carried from the last M5 cycle: a scan
        two seconds old is a different market, and the whole point of scanning
        continuously is to act on what is true now.
        """
        t0 = time.perf_counter()
        now = datetime.now(UTC)

        day = self.clock.to_broker(now).date()
        if self._day != day:
            self._day, self.trades_today = day, 0

        try:
            account = self.broker.account()
            quote = self.broker.quote(self.symbol)
            spec = self.broker.symbol_spec(self.symbol)
            positions = self.broker.positions(magic=self.settings.broker.magic)
        except BrokerError as exc:
            return ScanOutcome(
                ts=now,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error=f"broker: {exc}",
            )

        source = _BrokerBarSource(self.broker, self.settings)
        view = MarketView(source, self.symbol, now, quote)
        micro = self.micro.analyze(view)
        snap = self.pipeline.analyzer.analyze(
            view,
            self.context.macro,
            self.context.news,
            quote.spread_points(spec.point),
            25.0,
        )

        cycle = self.scalp.run(
            micro,
            snap,
            account=account,
            spec=spec,
            now=now,
            open_positions=positions,
            open_risk_pct=self._open_risk(positions, account.equity, spec),
            trades_today=self.trades_today,
            exposures=self._scalp_exposures(positions, account.equity, spec),
            broker_calc_profit=None,
        )

        self._persist_scalp(cycle)
        if cycle.executable is not None and cycle.executable.plan is not None:
            self._execute_scalp(cycle.executable, spec, now)

        return cycle.as_outcome(int((time.perf_counter() - t0) * 1000))

    def _scalp_exposures(self, positions, equity: float, spec) -> list[OpenExposure]:  # type: ignore[no-untyped-def]
        """Open positions as the correlation budgets see them."""
        out: list[OpenExposure] = []
        for p in positions:
            tracked = (
                self.positions.tracked(p.ticket) if hasattr(self.positions, "tracked") else None
            )
            plan = getattr(tracked, "plan", None)
            risk = self.settings.scalp.risk_pct
            out.append(
                OpenExposure(
                    direction=p.direction,
                    risk_pct=risk,
                    stop_price=p.stop_loss or p.price_open,
                    opened_at=p.opened_at,
                    model=getattr(plan, "strategy", "") or "",
                    liquidity_ref=None,
                )
            )
        return out

    def _scalp_decision(self, ev, now: datetime) -> Decision:  # type: ignore[no-untyped-def]
        """One evaluation as a journal entry, traded or not.

        Rejected scalps are journalled too. A scan that finds nothing and a scan whose
        every candidate was refused look identical from outside, and telling them apart
        is the whole reason the rejection ledger exists.
        """
        return Decision(
            ts=now,
            symbol=self.symbol,
            classification=(Classification.SCALP if ev.approved else Classification.NO_TRADE),
            mode=str(self.settings.mode),
            plan=ev.plan,
            score=ev.score,
            gates=tuple(ev.checks),
            reasons_against=() if ev.approved else (ev.rejected_by or "unknown",),
            config_hash=self.settings.config_hash(),
            git_sha=self.pipeline.git_sha,
        )

    def _persist_scalp(self, cycle) -> None:  # type: ignore[no-untyped-def]
        """Journal every evaluation. Never let a write failure stop the scan."""
        if not cycle.evaluations:
            return
        try:
            with self.db.session() as s:
                repos = Repositories(s)
                for ev in cycle.evaluations:
                    repos.decisions.save(self._scalp_decision(ev, cycle.ts))
                s.commit()
        except Exception as exc:
            log.error("scalp_persist_failed", error=f"{type(exc).__name__}: {exc}")

    def _execute_scalp(self, ev, spec, now: datetime) -> None:  # type: ignore[no-untyped-def]
        """Route an approved scalp through the SAME execution path as an A/A+ trade."""
        self._execute(self._scalp_decision(ev, now), spec, now)

    def _execute(self, decision, spec, now: datetime) -> None:  # type: ignore[no-untyped-def]
        tag = client_tag(decision.plan)
        outcome = self.orders.execute(decision, tag, spec, now, self.settings.broker.magic)
        log.info("execution_outcome", **{"outcome": outcome.log_line()})
        if outcome.ok and outcome.ticket:
            self.trades_today += 1
            for p in self.broker.positions(magic=self.settings.broker.magic):
                if p.ticket == outcome.ticket:
                    self.positions.adopt(outcome.ticket, decision.plan, p)
                    break
            self.notifier.info(
                "TRADE",
                f"{decision.classification} {decision.plan.direction} opened",
                f"{outcome.volume} lots at {outcome.fill_price}",
                rr=round(decision.plan.rr, 2),
                score=decision.score,
            )

    async def _reconcile_loop(self) -> None:
        interval = self.settings.execution.reconcile_interval_seconds
        while self.running:
            await asyncio.sleep(interval)
            try:
                with self.db.session() as s:
                    rows = [
                        {
                            "mt5_position": p.mt5_position,
                            "strategy": p.strategy,
                            "current_sl": float(p.current_sl or 0),
                            "volume": float(p.volume),
                            "remaining_volume": float(p.remaining_volume),
                        }
                        for p in Repositories(s).positions.get_open()
                    ]
                result = self.reconciler.reconcile(rows)
                self.health.report("reconciler", result.clean)
            except Exception as exc:
                log.error("reconcile_loop_error", error=str(exc))

    async def _command_loop(self) -> None:
        """Poll the operator command queue written by the dashboard.

        The dashboard cannot reach the broker; it records an intent and this is the only
        thing that acts on one. Kept on its own loop so an emergency FLATTEN is not
        waiting behind the M5 decision cycle.
        """
        interval = max(1, self.settings.dashboard.command_poll_seconds)
        while self.running:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self._drain_commands)
            except Exception as exc:
                log.error("command_loop_error", error=f"{type(exc).__name__}: {exc}")

    def _drain_commands(self) -> int:
        """Claim, execute and record. Returns the number of commands handled."""
        with self.db.session() as s:
            claimed = [
                (r.id, r.command, r.reason, r.operator)
                for r in Repositories(s).commands.claim_pending()
            ]
        for command_id, command, reason, operator in claimed:
            try:
                result = self._execute_command(command, reason, operator)
                ok = True
            except Exception as exc:
                result = f"{type(exc).__name__}: {exc}"
                ok = False
                log.error("operator_command_failed", command=command, error=result)
            with self.db.session() as s:
                Repositories(s).commands.complete(command_id, result, ok=ok)
            log.warning(
                "operator_command_executed",
                command=command,
                command_id=command_id,
                operator=operator,
                result=result,
                ok=ok,
            )
        return len(claimed)

    def _execute_command(self, command: str, reason: str, operator: str) -> str:
        now = datetime.now(UTC)
        if command == "HALT":
            self.kill_switch.trip(
                KillSwitchReason.MANUAL,
                f"halted from the dashboard by {operator}: {reason}",
                {"operator": operator},
                now,
            )
            return "kill switch tripped (MANUAL); no new positions will be opened"

        if command == "FLATTEN":
            # Halt first. Flattening without halting invites the engine to re-enter on
            # the next M5 close, which is the opposite of what the operator asked for.
            self.kill_switch.trip(
                KillSwitchReason.MANUAL,
                f"flatten requested from the dashboard by {operator}: {reason}",
                {"operator": operator},
                now,
            )
            positions = self.broker.positions(magic=self.settings.broker.magic)
            closed: list[int] = []
            failed: list[int] = []
            for p in positions:
                try:
                    res = self.broker.close_position(p.ticket)
                    (closed if res.ok else failed).append(p.ticket)
                except Exception as exc:
                    failed.append(p.ticket)
                    log.error("flatten_close_failed", ticket=p.ticket, error=str(exc))
                else:
                    self.positions.forget(p.ticket)
            if failed:
                # Say so loudly: the operator must be told the account is not flat.
                self.notifier.critical(
                    "FLATTEN",
                    "Flatten did not close every position",
                    f"closed {closed}, FAILED {failed} — check the terminal now",
                )
                raise BrokerError(f"closed {len(closed)}, failed to close {failed}")
            return f"closed {len(closed)} position(s): {closed}" if closed else "no open positions"

        raise ValueError(f"unknown operator command {command!r}")

    async def _context_loop(self) -> None:
        while self.running:
            try:
                await asyncio.to_thread(self._refresh_context)
            except Exception as exc:
                log.error("context_refresh_failed", error=str(exc))
            await asyncio.sleep(300)

    def _refresh_context(self) -> None:
        """Macro, calendar and news. Failures degrade rather than stop the engine."""
        from xauusd.core.analyzer import UNKNOWN_MACRO, UNKNOWN_NEWS

        now = datetime.now(UTC)
        macro, news = UNKNOWN_MACRO, UNKNOWN_NEWS
        try:
            from xauusd.data.providers.calendar_feed import (
                FallbackCalendarProvider,
                LayeredCalendarProvider,
            )
            from xauusd.intelligence.economic_calendar import CalendarFilter
            from xauusd.intelligence.news import NewsEngine

            provider = LayeredCalendarProvider([FallbackCalendarProvider()])
            events = provider.events(now - timedelta(hours=6), now + timedelta(hours=12))
            blackout = CalendarFilter(self.settings.news).evaluate(now, events)
            engine = NewsEngine(self.settings.news)
            news = engine.aggregate(
                [],
                now,
                calendar_blackout=blackout.active,
                calendar_reason=blackout.reason,
                calendar_until=blackout.until,
                next_event=(
                    (blackout.next_event.name, blackout.next_event.ts)
                    if blackout.next_event
                    else None
                ),
                feed_age_minutes=None,
            )
        except Exception as exc:
            log.warning("context_partial", error=str(exc))
        self.context = ContextCache(macro, news, now)

    # -- helpers -----------------------------------------------------------------------

    def _open_risk(self, positions, equity: float, spec) -> float:  # type: ignore[no-untyped-def]
        if not positions or equity <= 0:
            return 0.0
        total = sum(
            abs(p.entry_price - p.stop_loss) * spec.value_per_price_unit(p.volume)
            for p in positions
            if p.stop_loss
        )
        return float(total / equity)

    def _strategy_status(self) -> dict[str, ValidationStatus]:
        try:
            with self.db.session() as s:
                return {
                    r.strategy: ValidationStatus(r.status)
                    for r in Repositories(s).strategy_status.all()
                }
        except Exception:
            return {}

    def _persist_cycle(self, result) -> None:  # type: ignore[no-untyped-def]
        try:
            with self.db.session() as s:
                repos = Repositories(s)
                snap = result.snapshot
                repos.snapshots.save(
                    ts=snap.ts,
                    symbol=snap.symbol,
                    regime=str(snap.regime),
                    vol_regime=str(snap.volatility.vol_regime),
                    session=str(snap.session.session),
                    killzone=str(snap.session.killzone),
                    bias_d=str(snap.bias(Timeframe.D1)),
                    bias_h4=str(snap.bias(Timeframe.H4)),
                    bias_h1=str(snap.bias(Timeframe.H1)),
                    macro_bias=str(snap.macro.bias),
                    news_risk=str(snap.news.risk),
                    spread_points=snap.volatility.spread_points,
                    payload=snapshot_payload(snap),
                    config_hash=self.settings.config_hash(),
                    git_sha=self.pipeline.git_sha,
                )
                for d in result.decisions:
                    repos.decisions.save(d)
        except Exception as exc:
            log.error("persist_cycle_failed", error=str(exc))

    def _persist_order(self, tag: str, status: str, data: dict[str, Any]) -> None:
        log.info("order_state", tag=tag, status=status, **data)


class _BrokerBarSource:
    """Adapts the broker's bar API to the MarketView BarSource protocol."""

    def __init__(self, broker: Broker, settings: Settings) -> None:
        self.broker = broker
        self.settings = settings
        self._cache: dict[tuple[str, Timeframe], tuple[float, BarSeries]] = {}

    def series(self, symbol: str, tf: Timeframe) -> BarSeries:
        key = (symbol, tf)
        now = time.monotonic()
        hit = self._cache.get(key)
        # Cache for a fraction of the timeframe: an H4 series does not need refetching
        # every five minutes.
        ttl = min(tf.seconds / 4, 60.0)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        count = self.settings.data.bars_to_load.get(str(tf), 300)
        try:
            bars = self.broker.bars(symbol, tf, count)
        except BrokerError:
            return hit[1] if hit else BarSeries.empty(tf)
        series = BarSeries.from_bars(tf, bars)
        self._cache[key] = (now, series)
        return series
