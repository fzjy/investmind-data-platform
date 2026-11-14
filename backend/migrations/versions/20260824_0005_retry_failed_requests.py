"""Persist replayable request state and retry resolution timestamps."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260824_0005"
down_revision = "20260820_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(
            "job_failure",
            schema="ops",
        )
    }
    if "request_payload" not in existing:
        op.add_column(
            "job_failure",
            sa.Column(
                "request_payload",
                sa.JSON(),
                server_default=sa.text("'{}'::json"),
                nullable=False,
            ),
            schema="ops",
        )
    if "last_attempted_at" not in existing:
        op.add_column(
            "job_failure",
            sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
            schema="ops",
        )
    if "resolved_at" not in existing:
        op.add_column(
            "job_failure",
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            schema="ops",
        )


def downgrade() -> None:
    op.drop_column("job_failure", "resolved_at", schema="ops")
    op.drop_column("job_failure", "last_attempted_at", schema="ops")
    op.drop_column("job_failure", "request_payload", schema="ops")
