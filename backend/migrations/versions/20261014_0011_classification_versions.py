"""Isolate classification versions without changing existing node identity."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261014_0011"
down_revision = "20260929_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("classification", schema="core")}
    if "source" not in columns:
        op.add_column("classification", sa.Column("source", sa.String(64), nullable=True), schema="core")
    nodes = sa.table("classification", sa.column("id", sa.Integer),
        sa.column("data", sa.JSON), sa.column("source", sa.String), schema="core")
    for row in connection.execute(sa.select(nodes.c.id, nodes.c.data).where(nodes.c.source.is_(None))).mappings():
        value = row["data"].get("src") if isinstance(row["data"], dict) else None
        source = value.strip() if isinstance(value, str) and value.strip() else "legacy_unknown"
        connection.execute(nodes.update().where(nodes.c.id == row["id"]).values(source=source))
    op.alter_column("classification", "source", nullable=False, schema="core")
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("classification", schema="core")}
    if "uq_classification_provider_id" in constraints:
        op.drop_constraint("uq_classification_provider_id", "classification", schema="core", type_="unique")
    if "uq_classification_provider_source_id" not in constraints:
        op.create_unique_constraint("uq_classification_provider_source_id", "classification",
            ["provider", "source", "classification_id"], schema="core")
    if "data" not in {column["name"] for column in inspector.get_columns("classification_member", schema="core")}:
        op.add_column("classification_member", sa.Column("data", sa.JSON, nullable=False, server_default=sa.text("'{}'")), schema="core")


def downgrade() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(sa.text("SELECT 1 FROM core.classification GROUP BY provider, classification_id HAVING count(*) > 1 LIMIT 1")).first()
    if duplicates:
        raise RuntimeError("Cannot downgrade classification with multiple sources for the same code")
    op.drop_constraint("uq_classification_provider_source_id", "classification", schema="core", type_="unique")
    op.create_unique_constraint("uq_classification_provider_id", "classification", ["provider", "classification_id"], schema="core")
    op.drop_column("classification_member", "data", schema="core")
    op.drop_column("classification", "source", schema="core")
