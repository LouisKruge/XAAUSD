"""Repositories: the only place raw SQL/ORM queries live.

Two responsibilities that matter beyond CRUD:
  * `MacroRepository.value_as_of` enforces the vintage rule (release_ts <= as_of).
  * `DecisionRepository` is the explainability surface: why did / didn't the bot trade.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from xauusd.database.models import (
    AccountSnapshotRow,
    BacktestRunRow,
    BacktestTradeRow,
    BarRow,
    CalendarEventRow,
    ConfigVersionRow,
    DecisionRow,
    KillSwitchEventRow,
    MacroObservationRow,
    MacroSeriesRow,
    MarketSnapshotRow,
    NewsAssessmentRow,
    NewsItemRow,
    OrderRow,
    PositionEventRow,
    PositionRow,
    RiskStateRow,
    StrategyStatusRow,
    SymbolSpecRow,
    ValidationReportRow,
)
from xauusd.domain.enums import Classification, Direction, OrderStatus, Timeframe
from xauusd.domain.types import Bar, Decision, SymbolSpec


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; restore UTC so comparisons stay correct."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class BarRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert_many(
        self, symbol: str, timeframe: Timeframe, bars: Sequence[Bar], source: str = "mt5"
    ) -> int:
        existing = {
            r[0]
            for r in self.s.execute(
                select(BarRow.ts).where(
                    BarRow.symbol == symbol,
                    BarRow.timeframe == str(timeframe),
                    BarRow.source == source,
                    BarRow.ts.in_([b.ts for b in bars]),
                )
            )
        }
        existing = {_aware(t) for t in existing}
        added = 0
        for b in bars:
            if b.ts in existing:
                continue
            self.s.add(
                BarRow(
                    symbol=symbol,
                    timeframe=str(timeframe),
                    ts=b.ts,
                    source=source,
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    tick_volume=b.tick_volume,
                    real_volume=b.real_volume,
                    spread_points=b.spread_points,
                )
            )
            added += 1
        return added

    def load(
        self,
        symbol: str,
        timeframe: Timeframe,
        end: datetime | None = None,
        limit: int | None = None,
        start: datetime | None = None,
        source: str = "mt5",
    ) -> list[Bar]:
        stmt: Select[Any] = select(BarRow).where(
            BarRow.symbol == symbol,
            BarRow.timeframe == str(timeframe),
            BarRow.source == source,
        )
        if end is not None:
            stmt = stmt.where(BarRow.ts <= end)
        if start is not None:
            stmt = stmt.where(BarRow.ts >= start)
        stmt = stmt.order_by(BarRow.ts.desc())
        if limit:
            stmt = stmt.limit(limit)
        rows = list(self.s.execute(stmt).scalars())
        rows.reverse()
        return [
            Bar(
                ts=_aware(r.ts),  # type: ignore[arg-type]
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                tick_volume=r.tick_volume,
                real_volume=r.real_volume,
                spread_points=r.spread_points,
            )
            for r in rows
        ]

    def latest_ts(self, symbol: str, timeframe: Timeframe, source: str = "mt5") -> datetime | None:
        return _aware(
            self.s.execute(
                select(func.max(BarRow.ts)).where(
                    BarRow.symbol == symbol,
                    BarRow.timeframe == str(timeframe),
                    BarRow.source == source,
                )
            ).scalar()
        )

    def count(self, symbol: str, timeframe: Timeframe, source: str = "mt5") -> int:
        return int(
            self.s.execute(
                select(func.count())
                .select_from(BarRow)
                .where(
                    BarRow.symbol == symbol,
                    BarRow.timeframe == str(timeframe),
                    BarRow.source == source,
                )
            ).scalar()
            or 0
        )

    def find_gaps(
        self, symbol: str, timeframe: Timeframe, source: str = "mt5"
    ) -> list[tuple[datetime, datetime]]:
        """Return (before, after) pairs where more than one interval elapsed.

        Weekend gaps are expected for FX/metals and are excluded by ignoring gaps that
        span a Saturday.
        """
        rows = list(
            self.s.execute(
                select(BarRow.ts)
                .where(
                    BarRow.symbol == symbol,
                    BarRow.timeframe == str(timeframe),
                    BarRow.source == source,
                )
                .order_by(BarRow.ts)
            ).scalars()
        )
        step = timedelta(seconds=timeframe.seconds)
        gaps: list[tuple[datetime, datetime]] = []
        for prev, nxt in zip(rows, rows[1:]):
            p, n = _aware(prev), _aware(nxt)
            assert p and n
            if n - p > step * 1.5:
                spans_weekend = any(
                    (p + timedelta(days=d)).weekday() == 5 for d in range((n - p).days + 1)
                )
                if not spans_weekend:
                    gaps.append((p, n))
        return gaps


class SymbolSpecRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def save(self, broker: str, login: int, spec: SymbolSpec) -> str:
        h = spec.spec_hash()
        exists = self.s.execute(
            select(SymbolSpecRow.id).where(
                SymbolSpecRow.broker == broker,
                SymbolSpecRow.account_login == login,
                SymbolSpecRow.symbol == spec.symbol,
                SymbolSpecRow.spec_hash == h,
            )
        ).first()
        if not exists:
            self.s.add(
                SymbolSpecRow(
                    broker=broker,
                    account_login=login,
                    symbol=spec.symbol,
                    spec_hash=h,
                    raw=json.loads(
                        json.dumps(
                            spec.__dict__
                            if hasattr(spec, "__dict__")
                            else {
                                k: getattr(spec, k)
                                for k in spec.__slots__  # type: ignore[attr-defined]
                            },
                            default=str,
                        )
                    ),
                )
            )
        return h

    def latest_hash(self, broker: str, login: int, symbol: str) -> str | None:
        return self.s.execute(
            select(SymbolSpecRow.spec_hash)
            .where(
                SymbolSpecRow.broker == broker,
                SymbolSpecRow.account_login == login,
                SymbolSpecRow.symbol == symbol,
            )
            .order_by(SymbolSpecRow.observed_at.desc())
            .limit(1)
        ).scalar()


class MacroRepository:
    """Vintage-aware macro access. This class is the anti-leak boundary."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def ensure_series(
        self,
        series_id: str,
        provider: str = "FRED",
        name: str = "",
        units: str | None = None,
        frequency: str | None = None,
        gold_relevance: str | None = None,
    ) -> None:
        """Register a series. Observations carry a FK to this, so it must exist first."""
        if self.s.get(MacroSeriesRow, series_id) is None:
            self.s.add(
                MacroSeriesRow(
                    series_id=series_id,
                    provider=provider,
                    name=name or series_id,
                    units=units,
                    frequency=frequency,
                    gold_relevance=gold_relevance,
                )
            )
            self.s.flush()

    def add_observation(
        self,
        series_id: str,
        ref_date: datetime,
        release_ts: datetime,
        value: float | None,
        revision: int = 0,
    ) -> None:
        self.ensure_series(series_id)
        self.s.merge(
            MacroObservationRow(
                series_id=series_id,
                ref_date=ref_date,
                revision=revision,
                release_ts=release_ts,
                value=value,
            )
        )

    def value_as_of(self, series_id: str, as_of: datetime) -> tuple[float, datetime] | None:
        """Latest value that was PUBLICLY KNOWN at `as_of`.

        Filtering on release_ts (not ref_date) is what stops a revised print from
        leaking backwards into a backtest.
        """
        row = self.s.execute(
            select(MacroObservationRow)
            .where(
                MacroObservationRow.series_id == series_id,
                MacroObservationRow.release_ts <= as_of,
                MacroObservationRow.value.is_not(None),
            )
            .order_by(MacroObservationRow.ref_date.desc(), MacroObservationRow.revision.desc())
            .limit(1)
        ).scalar()
        if row is None or row.value is None:
            return None
        return float(row.value), _aware(row.ref_date)  # type: ignore[return-value]

    def series_as_of(
        self, series_id: str, as_of: datetime, lookback_days: int = 90
    ) -> list[tuple[datetime, float]]:
        """Point-in-time series: for each ref_date, the latest revision known at as_of."""
        start = as_of - timedelta(days=lookback_days)
        rows = self.s.execute(
            select(MacroObservationRow)
            .where(
                MacroObservationRow.series_id == series_id,
                MacroObservationRow.release_ts <= as_of,
                MacroObservationRow.ref_date >= start,
                MacroObservationRow.value.is_not(None),
            )
            .order_by(MacroObservationRow.ref_date, MacroObservationRow.revision)
        ).scalars()
        by_date: dict[datetime, float] = {}
        for r in rows:
            by_date[_aware(r.ref_date)] = float(r.value)  # type: ignore[index,arg-type]
        return sorted(by_date.items())


class CalendarRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert(self, **kwargs: Any) -> CalendarEventRow:
        src, ext = kwargs.get("source"), kwargs.get("external_id")
        row = None
        if ext:
            row = self.s.execute(
                select(CalendarEventRow).where(
                    CalendarEventRow.source == src, CalendarEventRow.external_id == ext
                )
            ).scalar()
        if row:
            for k, v in kwargs.items():
                setattr(row, k, v)
            row.updated_at = datetime.now(UTC)
        else:
            row = CalendarEventRow(**kwargs)
            self.s.add(row)
        return row

    def events_between(
        self, start: datetime, end: datetime, min_impact: str | None = None
    ) -> list[CalendarEventRow]:
        stmt = select(CalendarEventRow).where(
            CalendarEventRow.scheduled_ts >= start, CalendarEventRow.scheduled_ts <= end
        )
        if min_impact:
            order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
            allowed = order[order.index(min_impact) :]
            stmt = stmt.where(CalendarEventRow.impact.in_(allowed))
        return list(self.s.execute(stmt.order_by(CalendarEventRow.scheduled_ts)).scalars())

    def known_at(self, as_of: datetime, start: datetime, end: datetime) -> list[CalendarEventRow]:
        """Events whose SCHEDULE was known at `as_of`. Actuals are masked separately."""
        return list(
            self.s.execute(
                select(CalendarEventRow)
                .where(
                    CalendarEventRow.first_seen_at <= as_of,
                    CalendarEventRow.scheduled_ts >= start,
                    CalendarEventRow.scheduled_ts <= end,
                )
                .order_by(CalendarEventRow.scheduled_ts)
            ).scalars()
        )


class NewsRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def add_item(self, **kwargs: Any) -> NewsItemRow | None:
        h = kwargs["content_hash"]
        if self.s.execute(select(NewsItemRow.id).where(NewsItemRow.content_hash == h)).first():
            return None
        row = NewsItemRow(**kwargs)
        self.s.add(row)
        self.s.flush()
        return row

    def add_assessment(self, **kwargs: Any) -> NewsAssessmentRow:
        row = NewsAssessmentRow(**kwargs)
        self.s.add(row)
        return row

    def assessments_between(
        self, start: datetime, end: datetime
    ) -> list[tuple[NewsItemRow, NewsAssessmentRow]]:
        rows = self.s.execute(
            select(NewsItemRow, NewsAssessmentRow)
            .join(NewsAssessmentRow, NewsAssessmentRow.news_id == NewsItemRow.id)
            .where(
                NewsItemRow.published_ts >= start,
                NewsItemRow.published_ts <= end,
                NewsAssessmentRow.assessed_at <= end,
            )
            .order_by(NewsItemRow.published_ts.desc())
        ).all()
        return [(r[0], r[1]) for r in rows]


class DecisionRepository:
    """The explainability surface."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def save(self, d: Decision) -> int:
        plan = d.plan
        row = DecisionRow(
            ts=d.ts,
            symbol=d.symbol,
            snapshot_id=d.snapshot_id,
            strategy=plan.strategy if plan else None,
            strategy_version=plan.strategy_version if plan else None,
            direction=str(plan.direction) if plan else None,
            classification=str(d.classification),
            setup_score=d.score,
            score_breakdown=d.breakdown.as_dict() if d.breakdown else None,
            probability=d.probability,
            model_id=d.model_id,
            model_health=d.model_health,
            features=d.features,
            gate_trace=d.gate_trace(),
            blocking_gate=d.blocking_gate,
            all_blocking=list(d.all_blocking),
            reasons_for=list(d.reasons_for),
            reasons_against=list(d.reasons_against),
            planned_entry=plan.entry if plan else None,
            planned_sl=plan.stop_loss if plan else None,
            planned_tp1=plan.targets[0].price if plan else None,
            planned_tp2=plan.targets[-1].price if plan and len(plan.targets) > 1 else None,
            planned_rr=plan.rr if plan else None,
            planned_risk_pct=d.sizing.risk_pct if d.sizing else None,
            planned_lots=d.sizing.lots if d.sizing else None,
            sizing=d.sizing.as_dict() if d.sizing else None,
            invalidation=plan.invalidation if plan else None,
            mode=d.mode,
            config_hash=d.config_hash,
            git_sha=d.git_sha,
            latency_ms=d.latency_ms,
        )
        self.s.add(row)
        self.s.flush()
        return int(row.id)

    def recent(self, limit: int = 100, classification: str | None = None) -> list[DecisionRow]:
        stmt = select(DecisionRow).order_by(DecisionRow.ts.desc()).limit(limit)
        if classification:
            stmt = stmt.where(DecisionRow.classification == classification)
        return list(self.s.execute(stmt).scalars())

    def get(self, decision_id: int) -> DecisionRow | None:
        return self.s.get(DecisionRow, decision_id)

    def rejection_ledger(self, start: datetime, end: datetime) -> list[tuple[str, int]]:
        """Why the bot did not trade, aggregated. The most useful screen in the system."""
        rows = self.s.execute(
            select(DecisionRow.blocking_gate, func.count())
            .where(
                DecisionRow.ts >= start,
                DecisionRow.ts <= end,
                DecisionRow.classification == str(Classification.NO_TRADE),
            )
            .group_by(DecisionRow.blocking_gate)
            .order_by(func.count().desc())
        ).all()
        return [(r[0] or "NO_CANDIDATE", int(r[1])) for r in rows]

    def counts_by_classification(self, start: datetime, end: datetime) -> dict[str, int]:
        rows = self.s.execute(
            select(DecisionRow.classification, func.count())
            .where(DecisionRow.ts >= start, DecisionRow.ts <= end)
            .group_by(DecisionRow.classification)
        ).all()
        return {r[0]: int(r[1]) for r in rows}


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def create_intent(
        self,
        client_tag: str,
        magic: int,
        symbol: str,
        direction: Direction,
        volume: float,
        price: float,
        sl: float,
        tp: float | None,
        decision_id: int | None = None,
        raw_request: dict[str, Any] | None = None,
    ) -> OrderRow:
        row = OrderRow(
            client_tag=client_tag,
            magic=magic,
            symbol=symbol,
            side=str(direction),
            requested_volume=volume,
            requested_price=price,
            sl=sl,
            tp=tp,
            status=str(OrderStatus.INTENT),
            decision_id=decision_id,
            raw_request=raw_request,
        )
        self.s.add(row)
        self.s.flush()
        return row

    def by_tag(self, client_tag: str) -> OrderRow | None:
        return self.s.execute(select(OrderRow).where(OrderRow.client_tag == client_tag)).scalar()

    def update_status(
        self,
        order: OrderRow,
        status: OrderStatus,
        retcode: int | None = None,
        retcode_text: str | None = None,
        ticket: int | None = None,
        raw_result: dict[str, Any] | None = None,
    ) -> None:
        order.status = str(status)
        if retcode is not None:
            order.retcode = retcode
        if retcode_text:
            order.retcode_text = retcode_text[:128]
        if ticket:
            order.mt5_ticket = ticket
        if raw_result is not None:
            order.raw_result = raw_result
        if status is OrderStatus.SENT:
            order.sent_at = datetime.now(UTC)
        if status.is_terminal:
            order.confirmed_at = datetime.now(UTC)

    def unresolved(self) -> list[OrderRow]:
        """Orders in a non-terminal state. Reconciled before the engine may trade."""
        return list(
            self.s.execute(
                select(OrderRow).where(
                    OrderRow.status.in_(
                        [
                            str(OrderStatus.INTENT),
                            str(OrderStatus.SENT),
                            str(OrderStatus.RECONCILING),
                        ]
                    )
                )
            ).scalars()
        )


class PositionRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def open_position(self, **kwargs: Any) -> PositionRow:
        row = PositionRow(**kwargs)
        self.s.add(row)
        self.s.flush()
        return row

    def get_open(self, symbol: str | None = None) -> list[PositionRow]:
        stmt = select(PositionRow).where(PositionRow.closed_at.is_(None))
        if symbol:
            stmt = stmt.where(PositionRow.symbol == symbol)
        return list(self.s.execute(stmt).scalars())

    def by_ticket(self, ticket: int) -> PositionRow | None:
        return self.s.execute(
            select(PositionRow).where(PositionRow.mt5_position == ticket)
        ).scalar()

    def close(
        self,
        row: PositionRow,
        exit_price: float,
        exit_reason: str,
        gross_pnl: float,
        commission: float = 0.0,
        swap: float = 0.0,
        closed_at: datetime | None = None,
    ) -> None:
        row.closed_at = closed_at or datetime.now(UTC)
        row.exit_price = exit_price
        row.exit_reason = exit_reason
        row.gross_pnl = gross_pnl
        row.commission = commission
        row.swap = swap
        net = gross_pnl - abs(commission) + swap
        row.net_pnl = net
        row.r_multiple = net / float(row.risk_money) if float(row.risk_money) > 0 else 0.0

    def add_event(self, position_id: int, kind: str, **kwargs: Any) -> None:
        self.s.add(PositionEventRow(position_id=position_id, kind=kind, **kwargs))

    def closed_between(self, start: datetime, end: datetime) -> list[PositionRow]:
        return list(
            self.s.execute(
                select(PositionRow)
                .where(
                    PositionRow.closed_at.is_not(None),
                    PositionRow.closed_at >= start,
                    PositionRow.closed_at <= end,
                )
                .order_by(PositionRow.closed_at)
            ).scalars()
        )


class RiskRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def get_or_create(
        self, period_type: str, period_start: datetime, equity: float, limit_pct: float
    ) -> RiskStateRow:
        row = self.s.get(RiskStateRow, (period_type, period_start))
        if row is None:
            row = RiskStateRow(
                period_type=period_type,
                period_start=period_start,
                starting_equity=equity,
                peak_equity=equity,
                current_equity=equity,
                limit_pct=limit_pct,
            )
            self.s.add(row)
            self.s.flush()
        return row

    def log_kill_switch(
        self,
        action: str,
        reason_code: str,
        detail: str = "",
        context: dict[str, Any] | None = None,
        auto_clearable: bool = False,
        cleared_by: str | None = None,
    ) -> None:
        self.s.add(
            KillSwitchEventRow(
                action=action,
                reason_code=reason_code,
                detail=detail,
                context=context,
                auto_clearable=auto_clearable,
                cleared_by=cleared_by,
            )
        )

    def kill_switch_history(self, limit: int = 50) -> list[KillSwitchEventRow]:
        return list(
            self.s.execute(
                select(KillSwitchEventRow).order_by(KillSwitchEventRow.ts.desc()).limit(limit)
            ).scalars()
        )

    def snapshot_account(self, **kwargs: Any) -> None:
        self.s.merge(AccountSnapshotRow(**kwargs))

    def equity_curve(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        rows = self.s.execute(
            select(AccountSnapshotRow.ts, AccountSnapshotRow.equity)
            .where(AccountSnapshotRow.ts >= start, AccountSnapshotRow.ts <= end)
            .order_by(AccountSnapshotRow.ts)
        ).all()
        return [(_aware(r[0]), float(r[1])) for r in rows]  # type: ignore[misc]


class StrategyStatusRepository:
    """Gate on live routing."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def set_status(
        self,
        strategy: str,
        version: str,
        status: str,
        max_class: str = "A",
        report_id: int | None = None,
        approved_regimes: list[str] | None = None,
        approved_sessions: list[str] | None = None,
    ) -> None:
        row = self.s.get(StrategyStatusRow, (strategy, version))
        if row is None:
            row = StrategyStatusRow(strategy=strategy, strategy_version=version)
            self.s.add(row)
        row.status = status
        row.max_class = max_class
        row.validation_report_id = report_id
        row.approved_regimes = approved_regimes
        row.approved_sessions = approved_sessions
        row.updated_at = datetime.now(UTC)

    def get(self, strategy: str, version: str) -> StrategyStatusRow | None:
        return self.s.get(StrategyStatusRow, (strategy, version))

    def all(self) -> list[StrategyStatusRow]:
        return list(self.s.execute(select(StrategyStatusRow)).scalars())


class BacktestRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def save_run(self, **kwargs: Any) -> int:
        row = BacktestRunRow(**kwargs)
        self.s.add(row)
        self.s.flush()
        return int(row.id)

    def save_trades(self, run_id: int, trades: Sequence[dict[str, Any]]) -> None:
        for t in trades:
            self.s.add(BacktestTradeRow(run_id=run_id, **t))

    def save_validation(self, **kwargs: Any) -> int:
        row = ValidationReportRow(**kwargs)
        self.s.add(row)
        self.s.flush()
        return int(row.id)

    def runs(self, strategy: str | None = None, limit: int = 50) -> list[BacktestRunRow]:
        stmt = select(BacktestRunRow).order_by(BacktestRunRow.created_at.desc()).limit(limit)
        if strategy:
            stmt = stmt.where(BacktestRunRow.strategy == strategy)
        return list(self.s.execute(stmt).scalars())


class ConfigRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def record(self, config_hash: str, content: dict[str, Any], git_sha: str = "") -> None:
        if self.s.get(ConfigVersionRow, config_hash) is None:
            self.s.add(ConfigVersionRow(config_hash=config_hash, content=content, git_sha=git_sha))


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def save(self, **kwargs: Any) -> int:
        row = MarketSnapshotRow(**kwargs)
        self.s.add(row)
        self.s.flush()
        return int(row.id)

    def latest(self, symbol: str) -> MarketSnapshotRow | None:
        return self.s.execute(
            select(MarketSnapshotRow)
            .where(MarketSnapshotRow.symbol == symbol)
            .order_by(MarketSnapshotRow.ts.desc())
            .limit(1)
        ).scalar()


class Repositories:
    """Convenience aggregate so callers take one dependency, not twelve."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.bars = BarRepository(session)
        self.specs = SymbolSpecRepository(session)
        self.macro = MacroRepository(session)
        self.calendar = CalendarRepository(session)
        self.news = NewsRepository(session)
        self.decisions = DecisionRepository(session)
        self.orders = OrderRepository(session)
        self.positions = PositionRepository(session)
        self.risk = RiskRepository(session)
        self.strategy_status = StrategyStatusRepository(session)
        self.backtests = BacktestRepository(session)
        self.config = ConfigRepository(session)
        self.snapshots = SnapshotRepository(session)
