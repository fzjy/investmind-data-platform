from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL migration tests",
    ),
]

CONSTRAINT_NAME = "uq_risk_result_security_rule_date"


def test_risk_identity_migration_repairs_real_legacy_duplicates(
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
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE derived.risk_result "
                        f"DROP CONSTRAINT {CONSTRAINT_NAME}"
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO derived.risk_result (
                            id,
                            ts_code,
                            risk_id,
                            as_of,
                            status,
                            formula,
                            inputs,
                            missing_fields,
                            conclusion
                        ) VALUES
                            (
                                4101,
                                '600519.SH',
                                'valuationRisk',
                                '2026-08-24',
                                'normal',
                                'legacy lower id',
                                '{"value": 20}'::json,
                                '[]'::json,
                                'discard lower id'
                            ),
                            (
                                4102,
                                '600519.SH',
                                'valuationRisk',
                                '2026-08-24',
                                'risk',
                                'legacy highest id',
                                '{"value": 100}'::json,
                                '["pe_history"]'::json,
                                'keep highest id'
                            )
                        """
                    )
                )

            command.upgrade(config, "20260825_0006")

            with engine.connect() as connection:
                surviving = connection.execute(
                    text(
                        """
                        SELECT id, status, formula, inputs, missing_fields, conclusion
                        FROM derived.risk_result
                        WHERE ts_code = '600519.SH'
                          AND risk_id = 'valuationRisk'
                          AND as_of = '2026-08-24'
                        """
                    )
                ).one()
            assert surviving.id == 4102
            assert surviving.status == "risk"
            assert surviving.formula == "legacy highest id"
            assert surviving.inputs == {"value": 100}
            assert surviving.missing_fields == ["pe_history"]
            assert surviving.conclusion == "keep highest id"
            assert _risk_constraint_columns(engine) == (
                "ts_code",
                "risk_id",
                "as_of",
            )

            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO derived.risk_result (
                                id,
                                ts_code,
                                risk_id,
                                as_of,
                                status,
                                formula,
                                inputs,
                                missing_fields,
                                conclusion
                            ) VALUES (
                                4103,
                                '600519.SH',
                                'valuationRisk',
                                '2026-08-24',
                                'normal',
                                'must conflict',
                                '{}'::json,
                                '[]'::json,
                                'must conflict'
                            )
                            """
                        )
                    )

            command.downgrade(config, "20260824_0005")
            assert _risk_constraint_columns(engine) == (
                "ts_code",
                "risk_id",
                "as_of",
            )
            command.upgrade(config, "20260825_0006")
            assert _risk_constraint_columns(engine) == (
                "ts_code",
                "risk_id",
                "as_of",
            )
        finally:
            engine.dispose()


def _risk_constraint_columns(engine: Engine) -> tuple[str, ...]:
    constraints = inspect(engine).get_unique_constraints(
        "risk_result",
        schema="derived",
    )
    matches = [
        constraint
        for constraint in constraints
        if constraint["name"] == CONSTRAINT_NAME
    ]
    assert len(matches) == 1
    return tuple(str(column) for column in matches[0]["column_names"])
