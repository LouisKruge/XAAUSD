"""Operator commands.

The dashboard's HALT and FLATTEN used to be appended to a list in the API process that
nothing ever read. They now go here: the two processes are separate, a queued emergency
stop must survive a restart of either, and an instruction to close every position needs
a permanent record of who asked and what happened.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 0001 builds the schema with Base.metadata.create_all, so it reflects
    # whatever the models say at the time it runs: a database initialised after this
    # table was added to models.py already has it, one initialised before does not.
    # Both must reach the same place, so check rather than assume.
    bind = op.get_bind()
    if "operator_commands" in sa.inspect(bind).get_table_names():
        return

    op.create_table(
        "operator_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("command", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator", sa.String(64), nullable=False, server_default="dashboard"),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="QUEUED"),
        sa.Column("result", sa.Text(), nullable=True),
    )
    # The engine's poll is "the oldest queued command"; without this it is a table scan
    # on a table that only ever grows.
    op.create_index(
        "ix_operator_commands_status", "operator_commands", ["status", "queued_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "operator_commands" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_operator_commands_status", table_name="operator_commands")
    op.drop_table("operator_commands")
