"""Initial schema.

Creates every table from xauusd.database.models, then applies the PostgreSQL-only
TimescaleDB hypertables and indexes. The Timescale section is skipped on SQLite so
the same migration works for local development and tests.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Tables that become hypertables, with their time column and chunk interval.
HYPERTABLES = [
    ("bars", "ts", "7 days"),
    ("ticks", "ts", "1 day"),
    ("market_snapshots", "ts", "30 days"),
    ("decisions", "ts", "30 days"),
    ("account_snapshots", "ts", "30 days"),
]


def upgrade() -> None:
    from xauusd.database.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind)

    if bind.dialect.name != "postgresql":
        return  # SQLite: plain tables are enough

    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
    for table, column, interval in HYPERTABLES:
        op.execute(
            f"SELECT create_hypertable('{table}', '{column}', "
            f"chunk_time_interval => INTERVAL '{interval}', "
            f"if_not_exists => TRUE, migrate_data => TRUE)"
        )

    # Compress older market data; the decision journal is never compressed because it
    # is queried constantly by the dashboard and is the primary research asset.
    op.execute(
        "ALTER TABLE bars SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, timeframe, source')"
    )
    op.execute("SELECT add_compression_policy('bars', INTERVAL '30 days')")
    op.execute("SELECT add_retention_policy('ticks', INTERVAL '90 days')")

    # JSONB indexes for the explainability queries the dashboard runs.
    op.execute("CREATE INDEX IF NOT EXISTS ix_decisions_features ON decisions USING gin (features)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_decisions_gates ON decisions USING gin (gate_trace)")

    # A read-only role for the dashboard. The dashboard process must not be able to
    # write to the trading tables; its only write paths go through the engine.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'xauusd_readonly') THEN
            CREATE ROLE xauusd_readonly NOLOGIN;
          END IF;
        END $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO xauusd_readonly")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO xauusd_readonly")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO xauusd_readonly"
    )


def downgrade() -> None:
    from xauusd.database.models import Base

    Base.metadata.drop_all(op.get_bind())
