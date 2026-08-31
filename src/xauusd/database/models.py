"""SQLAlchemy models.

Portable between PostgreSQL (production, with TimescaleDB hypertables applied by a
migration) and SQLite (tests, local dev). No Postgres-only column types are used in the
model definitions; JSONB is applied via a variant so both work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres (indexable, typed), plain JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(20, 8)
Price = Numeric(20, 8)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[str]: JSONType}


# --------------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------------


class BarRow(Base):
    __tablename__ = "bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), primary_key=True, default="mt5")
    open: Mapped[float] = mapped_column(Price)
    high: Mapped[float] = mapped_column(Price)
    low: Mapped[float] = mapped_column(Price)
    close: Mapped[float] = mapped_column(Price)
    tick_volume: Mapped[int] = mapped_column(Integer, default=0)
    real_volume: Mapped[int] = mapped_column(Integer, default=0)
    spread_points: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_bars_lookup", "symbol", "timeframe", "ts"),)


class TickRow(Base):
    __tablename__ = "ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bid: Mapped[float] = mapped_column(Price)
    ask: Mapped[float] = mapped_column(Price)
    source: Mapped[str] = mapped_column(String(64), default="mt5")

    __table_args__ = (Index("ix_ticks_symbol_ts", "symbol", "ts"),)


class SymbolSpecRow(Base):
    __tablename__ = "symbol_specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker: Mapped[str] = mapped_column(String(128))
    account_login: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    spec_hash: Mapped[str] = mapped_column(String(32))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType)

    __table_args__ = (
        UniqueConstraint("broker", "account_login", "symbol", "spec_hash", name="uq_spec"),
    )


class SpreadStatRow(Base):
    __tablename__ = "spread_stats"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    median_points: Mapped[float] = mapped_column(Float)
    p95_points: Mapped[float] = mapped_column(Float)
    max_points: Mapped[float] = mapped_column(Float)
    sample_n: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------------------
# Exogenous data (vintage-aware)
# --------------------------------------------------------------------------------------


class MacroSeriesRow(Base):
    __tablename__ = "macro_series"

    series_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    units: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gold_relevance: Mapped[str | None] = mapped_column(Text, nullable=True)


class MacroObservationRow(Base):
    """release_ts is the anti-leak key: historical reads filter release_ts <= view.now."""

    __tablename__ = "macro_observations"

    series_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("macro_series.series_id"), primary_key=True
    )
    ref_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    release_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_macro_release", "series_id", "release_ts"),)


class CalendarEventRow(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    scheduled_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impact: Mapped[str] = mapped_column(String(16))
    gold_relevance: Mapped[int] = mapped_column(Integer, default=0)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_calendar_source_ext"),
        Index("ix_calendar_scheduled", "scheduled_ts"),
    )


class NewsItemRow(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64))
    published_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    headline: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)


class NewsAssessmentRow(Base):
    """Frozen at assessed_at. Never regenerated for historical bars."""

    __tablename__ = "news_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(Integer, ForeignKey("news_items.id"))
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assessor: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    importance: Mapped[int] = mapped_column(Integer)
    gold_relevance: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(16))
    uncertainty: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (UniqueConstraint("news_id", "assessor", name="uq_news_assessor"),)


# --------------------------------------------------------------------------------------
# Analysis state
# --------------------------------------------------------------------------------------


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(32))
    regime: Mapped[str] = mapped_column(String(24))
    vol_regime: Mapped[str] = mapped_column(String(16))
    session: Mapped[str] = mapped_column(String(16))
    killzone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bias_mn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bias_w: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bias_d: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bias_h4: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bias_h1: Mapped[str | None] = mapped_column(String(16), nullable=True)
    macro_bias: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    news_risk: Mapped[str] = mapped_column(String(16), default="MODERATE")
    spread_points: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    config_hash: Mapped[str] = mapped_column(String(32), default="")
    git_sha: Mapped[str] = mapped_column(String(40), default="")

    __table_args__ = (Index("ix_snapshot_ts", "symbol", "ts"),)


# --------------------------------------------------------------------------------------
# Decisions & trading — the audit spine
# --------------------------------------------------------------------------------------


class DecisionRow(Base):
    """Written on EVERY evaluation, trade or not. The system's most valuable table."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(32))
    snapshot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("market_snapshots.id"), nullable=True
    )
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    classification: Mapped[str] = mapped_column(String(16))
    setup_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_health: Mapped[str | None] = mapped_column(String(24), nullable=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    gate_trace: Mapped[dict[str, Any]] = mapped_column(JSONType, default=list)
    blocking_gate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    all_blocking: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    reasons_for: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    reasons_against: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    planned_entry: Mapped[float | None] = mapped_column(Price, nullable=True)
    planned_sl: Mapped[float | None] = mapped_column(Price, nullable=True)
    planned_tp1: Mapped[float | None] = mapped_column(Price, nullable=True)
    planned_tp2: Mapped[float | None] = mapped_column(Price, nullable=True)
    planned_rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    sizing: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(32), default="")
    git_sha: Mapped[str] = mapped_column(String(40), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_decisions_ts", "symbol", "ts"),
        Index("ix_decisions_class", "classification", "ts"),
        Index("ix_decisions_blocking", "blocking_gate", "ts"),
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decisions.id"), nullable=True
    )
    client_tag: Mapped[str] = mapped_column(String(32), unique=True)
    magic: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    requested_volume: Mapped[float] = mapped_column(Float)
    requested_price: Mapped[float | None] = mapped_column(Price, nullable=True)
    sl: Mapped[float | None] = mapped_column(Price, nullable=True)
    tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retcode_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_request: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    raw_result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    mt5_deal: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    mt5_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    volume: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Price)
    commission: Mapped[float] = mapped_column(Money, default=0)
    swap: Mapped[float] = mapped_column(Money, default=0)
    profit: Mapped[float] = mapped_column(Money, default=0)
    slippage_points: Mapped[float] = mapped_column(Float, default=0)
    entry_type: Mapped[str] = mapped_column(String(16), default="IN")


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mt5_position: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    decision_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decisions.id"), nullable=True
    )
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(16), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float] = mapped_column(Price)
    initial_sl: Mapped[float] = mapped_column(Price)
    initial_tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    current_sl: Mapped[float | None] = mapped_column(Price, nullable=True)
    current_tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    volume: Mapped[float] = mapped_column(Float)
    remaining_volume: Mapped[float] = mapped_column(Float)
    risk_money: Mapped[float] = mapped_column(Money)
    risk_pct: Mapped[float] = mapped_column(Float)
    planned_rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Price, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Money, nullable=True)
    commission: Mapped[float] = mapped_column(Money, default=0)
    swap: Mapped[float] = mapped_column(Money, default=0)
    net_pnl: Mapped[float | None] = mapped_column(Money, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(24), nullable=True)

    events: Mapped[list[PositionEventRow]] = relationship(back_populates="position")

    __table_args__ = (Index("ix_positions_open", "symbol", "closed_at"),)


class PositionEventRow(Base):
    __tablename__ = "position_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, ForeignKey("positions.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(24))
    old_sl: Mapped[float | None] = mapped_column(Price, nullable=True)
    new_sl: Mapped[float | None] = mapped_column(Price, nullable=True)
    old_tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    new_tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    volume_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    position: Mapped[PositionRow] = relationship(back_populates="events")


# --------------------------------------------------------------------------------------
# Risk & system state
# --------------------------------------------------------------------------------------


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    balance: Mapped[float] = mapped_column(Money)
    equity: Mapped[float] = mapped_column(Money)
    margin: Mapped[float] = mapped_column(Money, default=0)
    free_margin: Mapped[float] = mapped_column(Money, default=0)
    margin_level: Mapped[float] = mapped_column(Float, default=0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    open_risk_pct: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    mode: Mapped[str] = mapped_column(String(16), default="PAPER")


class RiskStateRow(Base):
    __tablename__ = "risk_state"

    period_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    starting_equity: Mapped[float] = mapped_column(Money)
    peak_equity: Mapped[float] = mapped_column(Money)
    current_equity: Mapped[float] = mapped_column(Money)
    realised_pnl: Mapped[float] = mapped_column(Money, default=0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0)
    limit_pct: Mapped[float] = mapped_column(Float)
    trades_taken: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    locked_out: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lockout_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class KillSwitchEventRow(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(8))
    reason_code: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_clearable: Mapped[bool] = mapped_column(Boolean, default=False)


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class SystemHealthRow(Base):
    __tablename__ = "system_health"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    config_hash: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    git_sha: Mapped[str] = mapped_column(String(40), default="")
    content: Mapped[dict[str, Any]] = mapped_column(JSONType)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------------------


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(24))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_source: Mapped[str] = mapped_column(String(64))
    data_hash: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONType)
    config_hash: Mapped[str] = mapped_column(String(32))
    git_sha: Mapped[str] = mapped_column(String(40), default="")
    cost_model: Mapped[dict[str, Any]] = mapped_column(JSONType)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("backtest_runs.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    classification: Mapped[str] = mapped_column(String(16))
    entry: Mapped[float] = mapped_column(Price)
    sl: Mapped[float] = mapped_column(Price)
    tp: Mapped[float | None] = mapped_column(Price, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Price, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(24), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


class ValidationReportRow(Base):
    __tablename__ = "validation_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    in_sample_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oos_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    walk_forward: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    monte_carlo: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    sensitivity: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    stress: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))
    gate_results: Mapped[dict[str, Any]] = mapped_column(JSONType)
    approved_regimes: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    approved_sessions: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class StrategyStatusRow(Base):
    """The live-routing gate. Enforced in code — a DEV strategy cannot reach the broker."""

    __tablename__ = "strategy_status"

    strategy: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_version: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="DEV")
    validation_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_class: Mapped[str] = mapped_column(String(16), default="A")
    approved_regimes: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    approved_sessions: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelRow(Base):
    __tablename__ = "models"

    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    feature_schema_hash: Mapped[str] = mapped_column(String(32))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType)
    calibration: Mapped[dict[str, Any]] = mapped_column(JSONType)
    artifact_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="CANDIDATE")


class ModelHealthRow(Base):
    __tablename__ = "model_health"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_trades: Mapped[int] = mapped_column(Integer)
    realised_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    predicted_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    psi: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), default="HEALTHY")


ALL_TABLES = Base.metadata
