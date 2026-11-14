"""Persist scheduler operational state and snapshot freshness metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260905_0008"
down_revision = "20260902_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("scheduler_heartbeat", "ops"):
        op.create_table(
            "scheduler_heartbeat",
            sa.Column("instance_id", sa.String(length=36), nullable=False),
            sa.Column("scheduler_name", sa.String(length=128), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "details",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("instance_id"),
            schema="ops",
        )
    if "ix_ops_scheduler_heartbeat_scheduler_name" not in _index_names(
        "scheduler_heartbeat",
        "ops",
    ):
        op.create_index(
            "ix_ops_scheduler_heartbeat_scheduler_name",
            "scheduler_heartbeat",
            ["scheduler_name"],
            schema="ops",
        )

    if not _has_table("scheduler_checkpoint", "ops"):
        op.create_table(
            "scheduler_checkpoint",
            sa.Column("checkpoint_key", sa.String(length=256), nullable=False),
            sa.Column("managed_from", sa.Date(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "details",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("checkpoint_key"),
            schema="ops",
        )

    if not _has_table("operational_alert", "ops"):
        op.create_table(
            "operational_alert",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("alert_key", sa.String(length=256), nullable=False),
            sa.Column("alert_type", sa.String(length=64), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("first_triggered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "details",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("alert_key", name="uq_operational_alert_alert_key"),
            schema="ops",
        )
    if "uq_operational_alert_alert_key" not in _unique_constraint_names(
        "operational_alert",
        "ops",
    ):
        op.create_unique_constraint(
            "uq_operational_alert_alert_key",
            "operational_alert",
            ["alert_key"],
            schema="ops",
        )

    if "stale_reason" not in _column_names("published_snapshot", "mart"):
        op.add_column(
            "published_snapshot",
            sa.Column("stale_reason", sa.Text(), nullable=True),
            schema="mart",
        )
    if "freshness_checked_at" not in _column_names("published_snapshot", "mart"):
        op.add_column(
            "published_snapshot",
            sa.Column("freshness_checked_at", sa.DateTime(timezone=True), nullable=True),
            schema="mart",
        )


def downgrade() -> None:
    if _has_table("published_snapshot", "mart"):
        if "freshness_checked_at" in _column_names("published_snapshot", "mart"):
            op.drop_column("published_snapshot", "freshness_checked_at", schema="mart")
        if "stale_reason" in _column_names("published_snapshot", "mart"):
            op.drop_column("published_snapshot", "stale_reason", schema="mart")

    if _has_table("operational_alert", "ops"):
        op.drop_table("operational_alert", schema="ops")
    if _has_table("scheduler_checkpoint", "ops"):
        op.drop_table("scheduler_checkpoint", schema="ops")
    if _has_table("scheduler_heartbeat", "ops"):
        if "ix_ops_scheduler_heartbeat_scheduler_name" in _index_names(
            "scheduler_heartbeat",
            "ops",
        ):
            op.drop_index(
                "ix_ops_scheduler_heartbeat_scheduler_name",
                table_name="scheduler_heartbeat",
                schema="ops",
            )
    if _has_table("scheduler_heartbeat", "ops"):
        op.drop_table("scheduler_heartbeat", schema="ops")


def _has_table(table_name: str, schema: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name, schema=schema)


def _column_names(table_name: str, schema: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name, schema=schema)
    }


def _index_names(table_name: str, schema: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name, schema=schema)
    }


def _unique_constraint_names(table_name: str, schema: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(
            table_name,
            schema=schema,
        )
    }
