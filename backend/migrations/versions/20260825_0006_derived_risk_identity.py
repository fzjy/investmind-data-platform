"""Enforce one risk result per security, rule, and as-of date."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260825_0006"
down_revision = "20260824_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY ts_code, risk_id, as_of
                        ORDER BY id DESC
                    ) AS row_number
                FROM derived.risk_result
            )
            DELETE FROM derived.risk_result
            WHERE id IN (
                SELECT id
                FROM ranked
                WHERE row_number > 1
            )
            """
        )
    )
    constraints = inspect(op.get_bind()).get_unique_constraints(
        "risk_result",
        schema="derived",
    )
    if not any(
        item["name"] == "uq_risk_result_security_rule_date"
        for item in constraints
    ):
        op.create_unique_constraint(
            "uq_risk_result_security_rule_date",
            "risk_result",
            ["ts_code", "risk_id", "as_of"],
            schema="derived",
        )


def downgrade() -> None:
    # 20260817_0002 may have created this constraint from mutable current
    # metadata, so the database cannot reliably identify 0006 as its source.
    # Preserve it rather than risk deleting a pre-existing identity constraint.
    pass
