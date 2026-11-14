"""Preserve one source payload occurrence for every ingestion batch request."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260820_0004"
down_revision = "20260817_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    constraints = inspect(op.get_bind()).get_unique_constraints(
        "source_payload",
        schema="raw",
    )
    if any(item["name"] == "uq_source_payload_content" for item in constraints):
        op.drop_constraint(
            "uq_source_payload_content",
            "source_payload",
            schema="raw",
            type_="unique",
        )


def downgrade() -> None:
    # Reintroducing global content-hash uniqueness would either fail once two
    # batches captured identical content or require deleting valid lineage.
    # The foundation migration uses current metadata, so a no-op also keeps a
    # fresh downgrade at the schema shape produced by revision 0003.
    pass
