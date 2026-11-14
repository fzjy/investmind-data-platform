from __future__ import annotations

from decimal import Decimal
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


def test_macro_state_migration_round_trip_preserves_existing_data(
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

        command.upgrade(config, "20260905_0008")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO core.macro_observation (
                            source, series_id, name, region, frequency, unit,
                            seasonal_adjustment, observed_at, released_at,
                            revision, value, source_url
                        ) VALUES (
                            'fred', 'us.cpi.yoy', 'US CPI YoY', 'US', 'monthly',
                            '%', 'seasonally_adjusted', '2026-08-01',
                            '2026-09-15T00:00:00+00:00', 'provider-v1', 3.1,
                            'https://example.test/cpi'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO mart.published_snapshot (
                            product, version, as_of, payload, stale
                        ) VALUES (
                            'macro-insights', 'before-0009', now(),
                            '{"status": "preserved"}'::json, false
                        )
                        """
                    )
                )

            command.upgrade(config, "20260920_0009")
            command.upgrade(config, "20260920_0009")

            with engine.begin() as connection:
                assert _current_revision(connection) == "20260920_0009"
                assert "macro_series_state" in _tables(connection, "ops")
                connection.execute(
                    text(
                        """
                        INSERT INTO ops.macro_series_state (
                            series_id, catalog_version, last_checked_at,
                            fallback_used, stale, consecutive_failures
                        ) VALUES (
                            'us.cpi.yoy', '2026-09-20-v1', now(), false, false, 0
                        )
                        """
                    )
                )

            command.downgrade(config, "20260905_0008")

            with engine.connect() as connection:
                assert _current_revision(connection) == "20260905_0008"
                assert "macro_series_state" not in _tables(connection, "ops")
                assert connection.scalar(
                    text("SELECT value FROM core.macro_observation WHERE series_id = 'us.cpi.yoy'")
                ) == Decimal("3.1000000000")
                assert connection.scalar(
                    text(
                        "SELECT version FROM mart.published_snapshot "
                        "WHERE product = 'macro-insights'"
                    )
                ) == "before-0009"

            command.upgrade(config, "20260920_0009")

            with engine.connect() as connection:
                assert _current_revision(connection) == "20260920_0009"
                assert "macro_series_state" in _tables(connection, "ops")
                assert connection.scalar(
                    text("SELECT count(*) FROM ops.macro_series_state")
                ) == 0
                assert connection.scalar(
                    text("SELECT count(*) FROM core.macro_observation")
                ) == 1
                assert connection.scalar(
                    text("SELECT count(*) FROM mart.published_snapshot")
                ) == 1
        finally:
            engine.dispose()


def _current_revision(connection: Connection) -> str:
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_macro_validity_migration_round_trip_preserves_history_and_published_data(monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker
    from app.macro.jobs.macro_sync_repository import MacroObservationRepository
    from app.jobs.sync_status import MacroStatusReader
    from app.config import Settings
    from app.macro.marts.macro import MacroRepository, MacroMartBuilder
    from backend.tests.jobs.test_macro_validity import AS_OF, BEFORE, CLOSED, SERIES, CATALOG, fetch_levels
    import app.config as config_module

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.chdir(PROJECT_ROOT)
        monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", tmp_path / "missing.env")
        monkeypatch.setenv("DANG_DATA_BACKEND", "database")
        monkeypatch.setenv("DANG_DATABASE_URL", database_url)
        monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
        config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
        command.upgrade(config, "20260920_0009")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(text("""INSERT INTO core.macro_observation (
                    source,series_id,name,region,frequency,unit,seasonal_adjustment,
                    observed_at,released_at,revision,value,source_url
                ) VALUES ('fred',:id,:name,'US','monthly','%','seasonally_adjusted',
                    '2026-08-01','2026-09-15T00:00:00Z','2026-09-15',50,:url)"""),
                    {"id": SERIES.id, "name": SERIES.name, "url": str(SERIES.sources[0].source_url)})
                connection.execute(text("""INSERT INTO ops.macro_series_state (
                    series_id,catalog_version,last_checked_at,last_success_at,
                    active_source,latest_observed_at,latest_released_at,fallback_used,stale,consecutive_failures
                ) VALUES (:id,:version,'2026-09-16T00:00:00Z','2026-09-16T00:00:00Z',
                    'fred','2026-08-01','2026-09-15T00:00:00Z',false,false,0)"""),
                    {"id": SERIES.id, "version": CATALOG.catalog_version})
                connection.execute(text("""INSERT INTO mart.published_snapshot (product,version,as_of,payload,stale)
                    VALUES ('macro-insights','before-validity','2026-09-16T00:00:00Z',CAST(:payload AS json),false)"""),
                    {"payload": '{"preserved":true}'})
            command.upgrade(config, "20260929_0010")
            command.upgrade(config, "20260929_0010")
            with engine.connect() as connection:
                assert _current_revision(connection) == "20260929_0010"
                column = next(item for item in inspect(connection).get_columns("macro_observation", schema="core") if item["name"] == "valid_until")
                assert column["nullable"] is True and column["type"].timezone is True
                assert connection.scalar(text("SELECT valid_until FROM core.macro_observation")) is None
            factory = sessionmaker(engine, expire_on_commit=False)
            repository = MacroObservationRepository(factory)
            # A same-value/same-vintage response closes the legacy open record.
            persisted = repository.persist(SERIES, fetch_levels(), AS_OF)
            assert (persisted.inserted, persisted.unchanged, persisted.rejected) == (0, 1, 0)
            assert repository.latest_visible(SERIES, BEFORE, AS_OF.date()) is not None
            assert repository.latest_visible(SERIES, CLOSED, AS_OF.date()) is None
            reader = MacroRepository(factory)
            assert reader.visible_observations((SERIES.id,), AS_OF) == ()
            for candidate in MacroMartBuilder(reader, catalog=CATALOG).build(AS_OF, "validity-pg"):
                status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == SERIES.id)
                assert status["stale"] is True and status["observedAt"] is None
            assert MacroStatusReader(factory, Settings(macro_sync_enabled=True), catalog=CATALOG).read(AS_OF)["series"]["fresh"] == 0
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT valid_until FROM core.macro_observation")) == CLOSED
            command.downgrade(config, "20260920_0009")
            with engine.connect() as connection:
                assert not any(item["name"] == "valid_until" for item in inspect(connection).get_columns("macro_observation", schema="core"))
                assert connection.scalar(text("SELECT value FROM core.macro_observation")) == Decimal("50")
                assert connection.scalar(text("SELECT count(*) FROM ops.macro_series_state")) == 1
                assert connection.scalar(text("SELECT version FROM mart.published_snapshot")) == "before-validity"
            command.upgrade(config, "20260929_0010")
            with engine.connect() as connection:
                assert _current_revision(connection) == "20260929_0010"
                assert connection.scalar(text("SELECT count(*) FROM core.macro_observation")) == 1
                assert connection.scalar(text("SELECT valid_until FROM core.macro_observation")) is None
        finally:
            engine.dispose()


def _tables(connection: Connection, schema: str) -> set[str]:
    return set(inspect(connection).get_table_names(schema=schema))
