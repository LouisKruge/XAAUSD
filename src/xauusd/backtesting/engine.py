"""Event-driven backtester.

The engine's whole purpose is that it is NOT a separate implementation. It drives the
same DecisionPipeline, the same strategies, scoring, gates, classifier and RiskGate that
run live. Only two things are substituted:

    MarketView  -> a cursor over preloaded history instead of a live cache
    Broker      -> SimBroker instead of Mt5Broker

Anything that differs beyond those two is a parity bug, and the parity replay test in
CI exists to catch exactly that.

Costs are modelled, not assumed: spread comes from the recorded per-bar spread,
commission from the account's real schedule, slippage from a fitted distribution, and
intrabar SL/TP ordering resolves conservatively unless M1 data proves the sequence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from xauusd.backtesting.metrics import Metrics, compute
from xauusd.config.settings import Settings
from xauusd.core.micro_structure import MicroAnalyzer
from xauusd.core.sessions import SessionEngine
from xauusd.data.marketview import InMemoryBarSource, MarketView
from xauusd.data.series import BarSeries
from xauusd.domain.enums import (
    Classification,
    Direction,
    ExitReason,
    Regime,
    Timeframe,
    ValidationStatus,
)
from xauusd.domain.types import (
    AccountState,
    ClosedTrade,
    Decision,
    MacroState,
    NewsState,
    OrderRequest,
    Quote,
    SymbolSpec,
)
from xauusd.engine.pipeline import DecisionPipeline, EngineState, client_tag
from xauusd.engine.scalp_pipeline import ScalpPipeline
from xauusd.execution.broker import BrokerHealth
from xauusd.execution.sim_broker import SimBroker, SimFillModel
from xauusd.monitoring.logging import get_logger
from xauusd.risk.correlation import OpenExposure
from xauusd.risk.drawdown import DrawdownGuard
from xauusd.risk.gate import RiskGate
from xauusd.risk.kill_switch import KillSwitch

log = get_logger(__name__)


@dataclass(slots=True)
class BacktestConfig:
    starting_equity: float = 10_000.0
    decision_timeframe: Timeframe = Timeframe.M5
    warmup_bars: int = 3000
    step: int = 1  # evaluate every Nth decision bar
    manage_on_m1: bool = False  # use M1 for intrabar SL/TP ordering when available
    progress_every: int = 0  # log progress every N bars, 0 = silent
    max_bars: int | None = None


@dataclass(slots=True)
class BacktestResult:
    trades: list[ClosedTrade]
    decisions: list[Decision]
    equity_curve: list[tuple[datetime, float]]
    metrics: Metrics
    period_start: datetime
    period_end: datetime
    cost_model: dict[str, Any]
    config_hash: str
    data_hash: str
    bars_evaluated: int
    wall_seconds: float
    rejection_ledger: dict[str, int] = field(default_factory=dict)

    @property
    def equity(self) -> list[float]:
        return [e for _, e in self.equity_curve]

    def summary(self) -> str:
        return (
            f"{self.period_start.date()} -> {self.period_end.date()}  "
            f"{self.bars_evaluated} bars, {len(self.decisions)} decisions\n"
            f"  {self.metrics.summary_line()}"
        )


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        spec: SymbolSpec,
        config: BacktestConfig | None = None,
        fill_model: SimFillModel | None = None,
        macro_provider: Callable[[datetime], MacroState | None] | None = None,
        news_provider: Callable[[datetime], NewsState | None] | None = None,
        strategy_status: dict[str, ValidationStatus] | None = None,
    ) -> None:
        self.settings = settings
        self.spec = spec
        self.cfg = config or BacktestConfig()
        self.fill_model = fill_model or SimFillModel()
        self.macro_provider = macro_provider
        self.news_provider = news_provider
        self.strategy_status = strategy_status or {}
        self.sessions = SessionEngine(settings.session)
        self._decision_by_tag: dict[str, Decision] = {}

    # -- main loop ---------------------------------------------------------------------

    def run(self, data: dict[Timeframe, BarSeries]) -> BacktestResult:
        t_start = time.perf_counter()
        self._decision_by_tag = {}
        cfg = self.cfg
        dtf = cfg.decision_timeframe
        base = data.get(dtf)
        if base is None or len(base) <= cfg.warmup_bars:
            raise ValueError(
                f"need more than {cfg.warmup_bars} {dtf} bars, got "
                f"{0 if base is None else len(base)}"
            )

        source = InMemoryBarSource()
        for tf, series in data.items():
            source.set(tf, series)

        broker = SimBroker(
            self.spec,
            cfg.starting_equity,
            fill_model=self.fill_model,
        )
        kill_switch = KillSwitch()
        drawdown = DrawdownGuard(self.settings.risk)
        risk_gate = RiskGate(self.settings, drawdown, kill_switch)
        pipeline = DecisionPipeline(self.settings, risk_gate=risk_gate)

        # The scalp engine, driven by the SAME SimBroker, the same RiskGate and the
        # same kill switch. Without this the scalp engine could trade live but never be
        # backtested, which is exactly backwards: it would reach real money without any
        # of the validation the deployment gate exists to demand.
        scalp = ScalpPipeline(self.settings, risk_gate=risk_gate)
        micro_analyzer = MicroAnalyzer(self.settings)

        m1 = data.get(Timeframe.M1) if cfg.manage_on_m1 else None

        decisions: list[Decision] = []
        equity_curve: list[tuple[datetime, float]] = []
        rejections: dict[str, int] = {}
        open_tags: set[str] = set()
        trades_today = 0
        current_day: date | None = None
        bars_in_market = 0

        end_index = (
            len(base) if cfg.max_bars is None else min(len(base), cfg.warmup_bars + cfg.max_bars)
        )

        for i in range(cfg.warmup_bars, end_index):
            bar = base.bar_at(i)
            # The decision instant is the bar's CLOSE: that is when its high and low
            # are known and when a live engine would wake.
            now = bar.ts + timedelta(seconds=dtf.seconds)

            # 1) advance the simulated broker and resolve any SL/TP inside this bar
            broker.set_time(now, bar, bar.spread_points or 25)
            m1_slice = self._m1_for(m1, bar.ts, dtf) if m1 is not None else None
            broker.step_bar(bar, m1_slice)

            equity = broker.account().equity
            equity_curve.append((now, equity))
            drawdown.update(now, equity)

            if broker.positions():
                bars_in_market += 1

            # reset the daily trade counter on the broker day
            day = now.date()
            if current_day is None or day != current_day:
                current_day = day
                trades_today = 0

            # 2) manage open positions (break-even, trailing, time stop)
            self._manage(broker, now, bar)

            if (i - cfg.warmup_bars) % max(cfg.step, 1) != 0:
                continue

            # 3) decide
            spread_pts = float(bar.spread_points or 25)
            half = spread_pts * self.spec.point / 2.0
            quote = Quote(now, bar.close - half, bar.close + half)
            view = MarketView(source, self.spec.symbol, now, quote)

            positions = broker.positions()
            open_risk = self._open_risk_pct(broker, positions, equity)
            state = EngineState(
                account=self._account(broker, now),
                spec=self.spec,
                health=BrokerHealth(True, True, True, 0.0),
                open_positions=positions,
                open_risk_pct=open_risk,
                trades_today=trades_today,
                existing_tags=set(open_tags),
                strategy_status=self.strategy_status,
            )
            result = pipeline.run(
                view,
                state,
                macro=self.macro_provider(now) if self.macro_provider else None,
                news=self.news_provider(now) if self.news_provider else None,
                spread_points=spread_pts,
                spread_median=self._median_spread(base, i),
            )
            decisions.extend(result.decisions)
            for d in result.decisions:
                if not d.is_trade:
                    rejections[d.blocking_gate or "NO_CANDIDATE"] = (
                        rejections.get(d.blocking_gate or "NO_CANDIDATE", 0) + 1
                    )

            # 4) execute the A/A+ decision
            if result.executable is not None:
                if self._execute(broker, result.executable, now, open_tags):
                    trades_today += 1

            # 5) the scalp engine, on the same bar and the same broker
            if self.settings.scalp.enabled and self.settings.scalp.enabled_models:
                micro = micro_analyzer.analyze(view)
                scalp_cycle = scalp.run(
                    micro,
                    result.snapshot,
                    account=self._account(broker, now),
                    spec=self.spec,
                    now=now,
                    open_positions=broker.positions(),
                    open_risk_pct=open_risk,
                    trades_today=trades_today,
                    exposures=self._scalp_exposures(broker.positions()),
                )
                if scalp_cycle.skipped:
                    rejections[f"scalp:{scalp_cycle.skipped}"] = (
                        rejections.get(f"scalp:{scalp_cycle.skipped}", 0) + 1
                    )
                for ev in scalp_cycle.evaluations:
                    if not ev.approved:
                        key = f"scalp:{ev.rejected_by}"
                        rejections[key] = rejections.get(key, 0) + 1
                if scalp_cycle.executable is not None:
                    sd = self._scalp_decision(scalp_cycle.executable, now)
                    decisions.append(sd)
                    if self._execute(broker, sd, now, open_tags):
                        trades_today += 1

            if cfg.progress_every and (i % cfg.progress_every == 0):
                log.info(
                    "backtest_progress",
                    bar=i,
                    of=end_index,
                    ts=now.isoformat(),
                    equity=round(equity, 2),
                    trades=len(broker.closed_trades),
                )

        broker.close_all(ExitReason.END_OF_TEST)
        trades = self._to_closed_trades(broker, decisions)

        period_start = base.bar_at(cfg.warmup_bars).ts
        period_end = base.bar_at(end_index - 1).ts
        metrics = compute(
            trades,
            starting_equity=cfg.starting_equity,
            equity_curve=[e for _, e in equity_curve] or None,
            risk_pct=self.settings.risk.risk_pct_a,
            period_days=(period_end - period_start).total_seconds() / 86400.0,
            bars_in_market=bars_in_market,
            total_bars=end_index - cfg.warmup_bars,
        )
        return BacktestResult(
            trades=trades,
            decisions=decisions,
            equity_curve=equity_curve,
            metrics=metrics,
            period_start=period_start,
            period_end=period_end,
            cost_model=self.fill_model.as_dict(),
            config_hash=self.settings.config_hash(),
            data_hash=data_hash(data),
            bars_evaluated=end_index - cfg.warmup_bars,
            wall_seconds=time.perf_counter() - t_start,
            rejection_ledger=dict(sorted(rejections.items(), key=lambda kv: -kv[1])),
        )

    # -- helpers -----------------------------------------------------------------------

    def _account(self, broker: SimBroker, now: datetime) -> AccountState:
        a = broker.account()
        return AccountState(
            login=0,
            currency=a.currency,
            balance=a.balance,
            equity=a.equity,
            margin=a.margin,
            free_margin=a.free_margin,
            margin_level=a.margin_level,
            ts=now,
        )

    @staticmethod
    def _median_spread(series: BarSeries, i: int, window: int = 500) -> float:
        lo = max(0, i - window)
        seg = series.spread[lo : i + 1]
        seg = seg[seg > 0]
        return float(np.median(seg)) if seg.size else 25.0

    def _open_risk_pct(self, broker: SimBroker, positions: list, equity: float) -> float:
        if not positions or equity <= 0:
            return 0.0
        total = 0.0
        for p in positions:
            if not p.stop_loss:
                continue
            dist = abs(p.entry_price - p.stop_loss)
            total += dist * self.spec.value_per_price_unit(p.volume)
        return total / equity

    @staticmethod
    def _m1_for(m1: BarSeries, bar_ts: datetime, dtf: Timeframe) -> list | None:
        t0 = int(bar_ts.timestamp())
        t1 = t0 + dtf.seconds
        mask = (m1.ts >= t0) & (m1.ts < t1)
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            return None
        return [m1.bar_at(int(j)) for j in idx]

    @staticmethod
    def _scalp_exposures(positions) -> list[OpenExposure]:  # type: ignore[no-untyped-def]
        """Open positions as the correlation budgets see them."""
        return [
            OpenExposure(
                direction=p.direction,
                risk_pct=0.0,
                stop_price=p.stop_loss or p.entry_price,
                opened_at=p.opened_at,
                model="",
                liquidity_ref=None,
            )
            for p in positions
        ]

    def _scalp_decision(self, ev, now: datetime) -> Decision:  # type: ignore[no-untyped-def]
        """An approved scalp as the Decision the execution path already understands.

        Carries the sizer's own result rather than recomputing it — a second sizing
        would be a second answer, free to disagree with the one the risk gate approved.
        """
        return Decision(
            ts=now,
            symbol=self.spec.symbol,
            classification=Classification.SCALP,
            mode=str(self.settings.mode),
            plan=ev.plan,
            score=ev.score,
            gates=tuple(ev.checks),
            sizing=ev.sizing,
            config_hash=self.settings.config_hash(),
        )

    def _execute(
        self, broker: SimBroker, decision: Decision, now: datetime, open_tags: set[str]
    ) -> bool:
        plan = decision.plan
        sizing = decision.sizing
        if plan is None or sizing is None or not sizing.approved:
            return False

        tag = client_tag(plan)
        if tag in open_tags:
            return False

        quote = broker.quote(self.spec.symbol)
        entry_now = quote.price_for(plan.direction)

        # Re-price at execution and re-check RR. Slippage against us reduces RR, and a
        # trade that no longer clears 1:2 is abandoned rather than chased.
        repriced = plan.with_entry(entry_now)
        if repriced.rr < self.settings.min_rr_for(decision.classification):
            return False
        drift = abs(entry_now - plan.entry) / plan.risk_distance if plan.risk_distance else 1.0
        if drift > self.settings.execution.max_entry_drift_r:
            return False

        req = OrderRequest(
            symbol=self.spec.symbol,
            direction=plan.direction,
            volume=sizing.lots,
            price=entry_now,
            stop_loss=plan.stop_loss,
            take_profit=plan.final_target.price,
            client_tag=tag,
            magic=self.settings.broker.magic,
            comment=f"{plan.strategy[:12]}:{tag}",
            max_slippage_points=self.settings.execution.max_slippage_points,
        )
        result = broker.send_market(req)
        if result.ok:
            open_tags.add(tag)
            self._decision_by_tag[tag] = decision
            return True
        return False

    def _manage(self, broker: SimBroker, now: datetime, bar) -> None:  # type: ignore[no-untyped-def]
        """Break-even, structural trail and time stop, using the same rules as live."""
        e = self.settings.execution
        for p in broker.positions():
            entry = p.entry_price
            sl = p.stop_loss
            if not sl:
                continue
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            price = bar.close
            r_now = (price - entry) * p.direction.sign / risk

            # time stop: a thesis that has not begun to work is tying up risk budget
            sim = broker._positions.get(p.ticket)
            if e.time_stop_bars and sim and sim.bars_held >= e.time_stop_bars:
                if r_now < e.time_stop_min_r:
                    broker.close_position(p.ticket)
                    continue

            if not e.trail_enabled and r_now < e.break_even_at_r:
                continue

            new_sl = sl
            if r_now >= e.break_even_at_r:
                be = entry + e.break_even_offset_r * risk * p.direction.sign
                new_sl = max(sl, be) if p.direction is Direction.LONG else min(sl, be)
            if e.trail_enabled and r_now >= e.trail_activate_r:
                trail = price - risk * p.direction.sign
                new_sl = max(new_sl, trail) if p.direction is Direction.LONG else min(new_sl, trail)

            # NEVER widen. The modify is skipped entirely if it would increase risk.
            improves = new_sl > sl if p.direction is Direction.LONG else new_sl < sl
            if improves:
                broker.modify_position(p.ticket, sl=new_sl)

    def _to_closed_trades(self, broker: SimBroker, decisions: list[Decision]) -> list[ClosedTrade]:
        out: list[ClosedTrade] = []
        for raw in broker.closed_trades:
            comment = str(raw.get("comment", ""))
            strategy, _, tag = comment.partition(":")
            # Map back by TAG, not by strategy name: several trades share a strategy and
            # keying on the name would attach every trade to the last decision.
            d = self._decision_by_tag.get(tag)
            opened = raw["opened_at"]
            out.append(
                ClosedTrade(
                    opened_at=opened,
                    closed_at=raw["closed_at"],
                    symbol=self.spec.symbol,
                    direction=raw["direction"],
                    strategy=strategy,
                    classification=(d.classification if d else Classification.A),
                    entry=raw["entry"],
                    initial_sl=raw["initial_sl"],
                    exit_price=raw["exit_price"],
                    volume=raw["volume"],
                    risk_money=raw["risk_money"],
                    gross_pnl=raw["gross_pnl"],
                    commission=raw["commission"],
                    swap=raw["swap"],
                    exit_reason=raw["exit_reason"],
                    mae_r=raw["mae_r"],
                    mfe_r=raw["mfe_r"],
                    bars_held=raw["bars_held"],
                    session=self.sessions.session_for(opened),
                    regime=Regime.RANGE,
                    score=d.score if d else None,
                    probability=d.probability if d else None,
                    planned_rr_at_entry=(d.plan.rr if d and d.plan else 0.0),
                )
            )
        return out


def data_hash(data: dict[Timeframe, BarSeries]) -> str:
    """Fingerprint the dataset so a validation report can never mix data sources."""
    import hashlib

    h = hashlib.blake2s(digest_size=8)
    for tf in sorted(data, key=lambda t: t.rank):
        s = data[tf]
        if not len(s):
            continue
        h.update(str(tf).encode())
        h.update(np.asarray([s.ts[0], s.ts[-1], len(s)], dtype=np.int64).tobytes())
        h.update(np.round(s.close[:: max(1, len(s) // 200)], 5).tobytes())
    return h.hexdigest()


def split_data(
    data: dict[Timeframe, BarSeries], fraction: float
) -> tuple[dict[Timeframe, BarSeries], dict[Timeframe, BarSeries]]:
    """Chronological in-sample / out-of-sample split at `fraction` of the base timeframe.

    Split by TIME, never randomly: a random split lets the model see the future of the
    same trend and is the classic way to manufacture an out-of-sample result that means
    nothing.
    """
    base_tf = min(data, key=lambda t: t.rank)
    base = data[base_tf]
    cut_ts = int(base.ts[int(len(base) * fraction)])
    first: dict[Timeframe, BarSeries] = {}
    second: dict[Timeframe, BarSeries] = {}
    for tf, s in data.items():
        idx = int(np.searchsorted(s.ts, cut_ts, side="right"))
        first[tf] = s.slice(0, idx)
        second[tf] = s.slice(max(0, idx), len(s))
    return first, second
