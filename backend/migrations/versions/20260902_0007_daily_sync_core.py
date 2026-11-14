"""Persist disclosure schedules and daily sync run details."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260902_0007"
down_revision = "20260825_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("disclosure_schedule", schema="core"):
        op.create_table(
            "disclosure_schedule",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=False),
            sa.Column("ann_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("pre_date", sa.Date(), nullable=True),
            sa.Column("actual_date", sa.Date(), nullable=True),
            sa.Column("modify_date", sa.Date(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(
                ["ts_code"],
                ["core.security.ts_code"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "ts_code",
                "end_date",
                name="uq_disclosure_schedule_period",
            ),
            schema="core",
        )

    shareholder_columns = {
        column["name"]
        for column in inspector.get_columns("shareholder_count", schema="core")
    }
    if "ann_date" not in shareholder_columns:
        op.add_column(
            "shareholder_count",
            sa.Column("ann_date", sa.Date(), nullable=True),
            schema="core",
        )

    job_run_columns = {
        column["name"]
        for column in inspector.get_columns("job_run", schema="ops")
    }
    if "details" not in job_run_columns:
        op.add_column(
            "job_run",
            sa.Column(
                "details",
                sa.JSON(),
                server_default=sa.text("'{}'::json"),
                nullable=False,
            ),
            schema="ops",
        )


def downgrade() -> None:
    op.drop_column("job_run", "details", schema="ops")
    op.drop_column("shareholder_count", "ann_date", schema="core")
    op.drop_table("disclosure_schedule", schema="core")
