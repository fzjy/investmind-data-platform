from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL migration tests",
    ),
]


def test_retry_migration_upgrades_fresh_and_legacy_job_failure_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[3]
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.chdir(project_root)
        monkeypatch.setattr(
            config_module,
            "PROJECT_ENV_FILE",
            tmp_path / "missing-project.env",
        )
        monkeypatch.setenv("DANG_DATA_BACKEND", "database")
        monkeypatch.setenv("DANG_DATABASE_URL", database_url)
        monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
        config = Config(str(project_root / "backend" / "alembic.ini"))

        command.upgrade(config, "20260824_0005")
        command.downgrade(config, "20260820_0004")

        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO ops.job_run (
                        id,
                        job_name,
                        business_key,
                        status,
                        request_count,
                        row_count
                    ) VALUES (
                        'legacy-run',
                        'backfill',
                        '2026-08-17:standard',
                        'failed',
                        0,
                        0
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ops.job_failure (
                        job_run_id,
                        dataset,
                        request_key,
                        error_code,
                        message,
                        retryable,
                        attempts
                    ) VALUES (
                        'legacy-run',
                        'daily',
                        'daily:600519.SH',
                        'TushareTransientError',
                        'temporary outage',
                        true,
                        1
                    )
                    """
                )
            )
        engine.dispose()

        command.upgrade(config, "20260824_0005")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT request_payload, last_attempted_at, resolved_at
                    FROM ops.job_failure
                    WHERE job_run_id = 'legacy-run'
                    """
                )
            ).one()
        engine.dispose()

    assert row.request_payload == {}
    assert row.last_attempted_at is None
    assert row.resolved_at is None
