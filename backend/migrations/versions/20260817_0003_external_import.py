"""Add external file, macro observation and content records."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.db.base import ExternalBase
from app.db import models  # noqa: F401


revision = "20260817_0003"
down_revision = "20260817_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for schema in ("raw", "core"):
        bind.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    ExternalBase.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    ExternalBase.metadata.drop_all(bind)
