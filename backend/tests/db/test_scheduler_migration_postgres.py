from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from backend.tests.e2e.test_daily_sync_postgres import (
    PROJECT_ROOT,
    Config,
    PostgresContainer,
    command,
)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL migration tests",
    ),
]


def test_scheduler_operations_migration_creates_expected_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.config as config_module

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.chdir(PROJECT_ROOT)
        monkeypatch.setattr(
            config_module,
            "PROJECT_ENV_FILE",
            tmp_path / "missing-project.env",
        )
        monkeypatch.setenv("DANG_DATA_BACKEND", "database")
        monkeypatch.setenv("DANG_DATABASE_URL", database_url)
        monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
        config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))

        command.upgrade(config, "20260902_0007")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO mart.published_snapshot (product, version, as_of, payload, stale)
                        VALUES ('market', 'before-0008', now(), '{}'::json, false)
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO ops.scheduler_heartbeat (
                            instance_id,
                            scheduler_name,
                            state,
                            started_at,
                            last_heartbeat_at,
                            next_run_at,
                            details
                        )
                        VALUES (
                            'bootstrap-instance',
                            'daily-sync',
                            'running',
                            now(),
                            now(),
                            NULL,
                            '{"source": "bootstrap-0007"}'::json
                        )
                        """
                    )
                )

            command.upgrade(config, "20260905_0008")
            command.upgrade(config, "20260905_0008")

            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT current_setting('server_version_num')::int / 10000")
                ) == 17
                assert _current_revision(connection) == "20260905_0008"
                assert {
                    "scheduler_heartbeat",
                    "scheduler_checkpoint",
                    "operational_alert",
                } <= _tables(connection, "ops")
                assert "ix_ops_scheduler_heartbeat_scheduler_name" in _indexes(
                    connection, "ops", "scheduler_heartbeat"
                )
                assert "uq_operational_alert_alert_key" in _unique_constraints(
                    connection, "ops", "operational_alert"
                )
                assert {"stale_reason", "freshness_checked_at"} <= _columns(
                    connection, "mart", "published_snapshot"
                )
                assert {
                    "instance_id",
                    "scheduler_name",
                    "state",
                    "started_at",
                    "last_heartbeat_at",
                    "next_run_at",
                    "details",
                } <= _columns(connection, "ops", "scheduler_heartbeat")
                assert {
                    "checkpoint_key",
                    "managed_from",
                    "created_at",
                    "last_evaluated_at",
                    "details",
                } <= _columns(connection, "ops", "scheduler_checkpoint")
                assert {
                    "id",
                    "alert_key",
                    "alert_type",
                    "severity",
                    "status",
                    "first_triggered_at",
                    "last_triggered_at",
                    "resolved_at",
                    "details",
                } <= _columns(connection, "ops", "operational_alert")
                snapshot = connection.execute(
                    text(
                        """
                        SELECT product, version, stale, stale_reason, freshness_checked_at
                        FROM mart.published_snapshot
                        WHERE product = 'market'
                        """
                    )
                ).one()
                heartbeat = connection.execute(
                    text(
                        """
                        SELECT scheduler_name, state, details
                        FROM ops.scheduler_heartbeat
                        WHERE instance_id = 'bootstrap-instance'
                        """
                    )
                ).one()

            assert snapshot == ("market", "before-0008", False, None, None)
            assert heartbeat == (
                "daily-sync",
                "running",
                {"source": "bootstrap-0007"},
            )

            with engine.begin() as connection:
                connection.execute(
                    text("DROP INDEX ops.ix_ops_scheduler_heartbeat_scheduler_name")
                )
                connection.execute(text("DROP TABLE ops.scheduler_checkpoint"))
                connection.execute(
                    text(
                        """
                        ALTER TABLE mart.published_snapshot
                        DROP COLUMN freshness_checked_at
                        """
                    )
                )

            command.downgrade(config, "20260902_0007")

            with engine.connect() as connection:
                assert _current_revision(connection) == "20260902_0007"
                assert {
                    "scheduler_heartbeat",
                    "scheduler_checkpoint",
                    "operational_alert",
                }.isdisjoint(_tables(connection, "ops"))
                assert {"stale_reason", "freshness_checked_at"}.isdisjoint(
                    _columns(connection, "mart", "published_snapshot")
                )
                restored_snapshot = connection.execute(
                    text(
                        """
                        SELECT product, version, stale
                        FROM mart.published_snapshot
                        WHERE product = 'market'
                        """
                    )
                ).one()

            assert restored_snapshot == ("market", "before-0008", False)

            command.upgrade(config, "20260905_0008")

            with engine.connect() as connection:
                assert _current_revision(connection) == "20260905_0008"
                assert {
                    "scheduler_heartbeat",
                    "scheduler_checkpoint",
                    "operational_alert",
                } <= _tables(connection, "ops")
                assert "ix_ops_scheduler_heartbeat_scheduler_name" in _indexes(
                    connection, "ops", "scheduler_heartbeat"
                )
                assert "uq_operational_alert_alert_key" in _unique_constraints(
                    connection, "ops", "operational_alert"
                )
                assert {"stale_reason", "freshness_checked_at"} <= _columns(
                    connection, "mart", "published_snapshot"
                )
                upgraded_snapshot = connection.execute(
                    text(
                        """
                        SELECT product, version, stale, stale_reason, freshness_checked_at
                        FROM mart.published_snapshot
                        WHERE product = 'market'
                        """
                    )
                ).one()

            assert upgraded_snapshot == (
                "market",
                "before-0008",
                False,
                None,
                None,
            )
        finally:
            engine.dispose()


def _current_revision(connection: Connection) -> str:
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _columns(connection: Connection, schema: str, table: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table, schema=schema)}


def _tables(connection: Connection, schema: str) -> set[str]:
    return set(inspect(connection).get_table_names(schema=schema))


def _indexes(connection: Connection, schema: str, table: str) -> set[str]:
    return {index["name"] for index in inspect(connection).get_indexes(table, schema=schema)}


def _unique_constraints(connection: Connection, schema: str, table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints(table, schema=schema)
    }
