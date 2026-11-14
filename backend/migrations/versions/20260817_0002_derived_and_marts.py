"""Add derived metrics and page marts."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.db.base import DerivedBase, MartBase
from app.db import models  # noqa: F401


revision = "20260817_0002"
down_revision = "20260817_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for schema in ("derived", "mart"):
            bind.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    else:
        bind = bind.execution_options(schema_translate_map={"raw": None, "core": None, "ops": None, "derived": None, "mart": None})
    DerivedBase.metadata.create_all(bind)
    MartBase.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for schema in ("mart", "derived"):
            bind.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    else:
        bind = bind.execution_options(schema_translate_map={"raw": None, "core": None, "ops": None, "derived": None, "mart": None})
        MartBase.metadata.drop_all(bind)
        DerivedBase.metadata.drop_all(bind)
