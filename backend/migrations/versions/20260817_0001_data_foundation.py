from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.db.base import Base
from app.db import models  # noqa: F401


revision = "20260817_0001"
down_revision = None
branch_labels = None
depends_on = None

# 当前metadata含后续迁移独占表，历史入口不得提前创建或接管其所有权。
OWNED_LATER = {'ops.news_delivery', 'core.market_daily_quote', 'ops.enterprise_receivable_attempt',
    'raw.enterprise_receivable_response', 'core.enterprise_receivable_supplement',
    'ops.enterprise_order_attempt','raw.enterprise_order_response','core.enterprise_order_supplement',
    'ops.enterprise_rea_plan','ops.enterprise_rea_attempt','raw.enterprise_rea_response','core.enterprise_rea_supplement'}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for schema in ("raw", "core", "ops"):
            bind.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    else:
        bind = bind.execution_options(
            schema_translate_map={"raw": None, "core": None, "ops": None}
        )
    tables = [table for table in Base.metadata.sorted_tables if table.fullname not in OWNED_LATER]
    Base.metadata.create_all(bind, tables=tables)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        Base.metadata.drop_all(bind, tables=[table for table in Base.metadata.sorted_tables if table.fullname not in OWNED_LATER])
        for schema in ("ops", "core", "raw"):
            bind.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    else:
        bind = bind.execution_options(
            schema_translate_map={"raw": None, "core": None, "ops": None}
        )
        Base.metadata.drop_all(bind, tables=[table for table in Base.metadata.sorted_tables if table.fullname not in OWNED_LATER])
