from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL migration tests",
    ),
]


def test_daily_sync_migration_preserves_legacy_rows_and_downgrades_cleanly(
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

        command.upgrade(config, "20260825_0006")
        engine = create_engine(database_url)
        try:
            _restore_pre_11a_schema(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO core.security (ts_code, symbol, name, is_st, data, source)
                        VALUES ('600519.SH', '600519', '贵州茅台', false, '{}'::json, 'tushare')
                        """
                    )
                )
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
                            'pre-11a-run',
                            'daily-history',
                            '2026-09-01',
                            'completed',
                            1,
                            1
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO core.shareholder_count (
                            ts_code,
                            end_date,
                            holder_num,
                            data
                        ) VALUES (
                            '600519.SH',
                            '2026-06-30',
                            123456,
                            '{"holder_num": "123456"}'::json
                        )
                        """
                    )
                )

            command.upgrade(config, "20260902_0007")

            with engine.begin() as connection:
                job_run = connection.execute(
                    text(
                        """
                        SELECT details
                        FROM ops.job_run
                        WHERE id = 'pre-11a-run'
                        """
                    )
                ).one()
                shareholder = connection.execute(
                    text(
                        """
                        SELECT ann_date, holder_num
                        FROM core.shareholder_count
                        WHERE ts_code = '600519.SH'
                          AND end_date = '2026-06-30'
                        """
                    )
                ).one()
                connection.execute(
                    text(
                        """
                        INSERT INTO core.disclosure_schedule (
                            ts_code,
                            ann_date,
                            end_date,
                            pre_date,
                            actual_date,
                            modify_date,
                            data
                        ) VALUES (
                            '600519.SH',
                            '2026-08-20',
                            '2026-06-30',
                            '2026-08-30',
                            '2026-09-02',
                            '2026-08-31',
                            '{"actual_date": "20260902"}'::json
                        )
                        """
                    )
                )

            assert job_run.details == {}
            assert shareholder.ann_date is None
            assert shareholder.holder_num == 123456
            before_downgrade = _schema_shape(engine)

            command.downgrade(config, "20260825_0006")

            after_downgrade = _schema_shape(engine)
            assert before_downgrade[0] - after_downgrade[0] == {
                ("core", "disclosure_schedule")
            }
            assert after_downgrade[0] == before_downgrade[0] - {
                ("core", "disclosure_schedule")
            }
            assert before_downgrade[1] - after_downgrade[1] == {
                ("core", "shareholder_count", "ann_date"),
                ("ops", "job_run", "details"),
                *{
                    column
                    for column in before_downgrade[1]
                    if column[:2] == ("core", "disclosure_schedule")
                },
            }
            assert after_downgrade[1] == {
                column
                for column in before_downgrade[1]
                if column[:2] != ("core", "disclosure_schedule")
                and column
                not in {
                    ("core", "shareholder_count", "ann_date"),
                    ("ops", "job_run", "details"),
                }
            }
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT count(*) FROM ops.job_run WHERE id = 'pre-11a-run'")
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM core.shareholder_count
                        WHERE ts_code = '600519.SH'
                          AND end_date = '2026-06-30'
                        """
                    )
                ).scalar_one() == 1
        finally:
            engine.dispose()


def _restore_pre_11a_schema(engine: Engine) -> None:
    """Undo mutable-foundation metadata leakage before testing revision 0007."""
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS core.disclosure_schedule"))
        connection.execute(
            text("ALTER TABLE core.shareholder_count DROP COLUMN IF EXISTS ann_date")
        )
        connection.execute(
            text("ALTER TABLE ops.job_run DROP COLUMN IF EXISTS details")
        )


def _schema_shape(
    engine: Engine,
) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]]]:
    inspector = inspect(engine)
    tables = {
        (schema, table)
        for schema in ("core", "ops")
        for table in inspector.get_table_names(schema=schema)
    }
    columns = {
        (schema, table, str(column["name"]))
        for schema, table in tables
        for column in inspector.get_columns(table, schema=schema)
    }
    return tables, columns
