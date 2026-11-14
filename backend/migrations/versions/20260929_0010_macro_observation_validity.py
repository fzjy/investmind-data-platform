"""Persist the exclusive validity boundary of a macro revision."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260929_0010"
down_revision = "20260920_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_column():
        op.add_column("macro_observation", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True), schema="core")


def downgrade() -> None:
    if _has_column():
        op.drop_column("macro_observation", "valid_until", schema="core")


def _has_column() -> bool:
    return any(column["name"] == "valid_until" for column in
               sa.inspect(op.get_bind()).get_columns("macro_observation", schema="core"))
