"""Persist per-series macro synchronization state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260920_0009"
down_revision = "20260905_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        "macro_series_state",
        sa.Column("series_id", sa.String(length=128), nullable=False),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_observed_at", sa.Date(), nullable=True),
        sa.Column("latest_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_source", sa.String(length=128), nullable=True),
        sa.Column(
            "fallback_used",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "stale",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("series_id"),
        schema="ops",
    )


def downgrade() -> None:
    if _has_table():
        op.drop_table("macro_series_state", schema="ops")


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(
        "macro_series_state",
        schema="ops",
    )
