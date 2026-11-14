from __future__ import annotations

from collections.abc import Iterator
from datetime import date
import os

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from app.jobs.locks import (
    PostgresAdvisoryJobLock,
    daily_sync_lock_key,
    data_publication_lock_key,
    lock_key,
)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL lock tests",
    ),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        engine = create_engine(
            postgres.get_connection_url(driver="psycopg"),
            poolclass=NullPool,
        )
        try:
            yield engine
        finally:
            engine.dispose()


def test_same_key_is_exclusive_across_lock_instances(postgres_engine: Engine) -> None:
    first = PostgresAdvisoryJobLock(postgres_engine)
    second = PostgresAdvisoryJobLock(postgres_engine)
    value = "sync-daily:2026-09-02"

    try:
        assert first.try_acquire(value) is True
        assert second.try_acquire(value) is False
    finally:
        first.release(value)
        second.release(value)


def test_different_business_dates_can_be_held_together(
    postgres_engine: Engine,
) -> None:
    first = PostgresAdvisoryJobLock(postgres_engine)
    second = PostgresAdvisoryJobLock(postgres_engine)
    first_value = "sync-daily:2026-09-02"
    second_value = "sync-daily:2026-09-03"

    try:
        assert first.try_acquire(first_value) is True
        assert second.try_acquire(second_value) is True
    finally:
        first.release(first_value)
        second.release(second_value)


def test_global_publication_lock_conflicts_across_different_business_dates(
    postgres_engine: Engine,
) -> None:
    daily = PostgresAdvisoryJobLock(postgres_engine)
    historical = PostgresAdvisoryJobLock(postgres_engine)
    daily_date_key = daily_sync_lock_key(date(2026, 9, 14))
    historical_date_key = daily_sync_lock_key(date(2026, 9, 12))
    publication_key = data_publication_lock_key()

    try:
        assert daily.try_acquire(daily_date_key) is True
        assert historical.try_acquire(historical_date_key) is True
        assert daily.try_acquire(publication_key) is True
        assert historical.try_acquire(publication_key) is False
    finally:
        daily.release(publication_key)
        historical.release(publication_key)
        daily.release(daily_date_key)
        historical.release(historical_date_key)


def test_release_permits_reacquisition(postgres_engine: Engine) -> None:
    first = PostgresAdvisoryJobLock(postgres_engine)
    second = PostgresAdvisoryJobLock(postgres_engine)
    value = "sync-daily:2026-09-02"

    assert first.try_acquire(value) is True
    first.release(value)
    try:
        assert second.try_acquire(value) is True
    finally:
        second.release(value)


def test_same_instance_does_not_overwrite_active_connection(
    postgres_engine: Engine,
) -> None:
    lock = PostgresAdvisoryJobLock(postgres_engine)
    value = "sync-daily:2026-09-02"

    try:
        assert lock.try_acquire(value) is True
        assert lock.try_acquire(value) is False
    finally:
        lock.release(value)


def test_closing_raw_session_releases_advisory_lock(postgres_engine: Engine) -> None:
    value = "sync-daily:2026-09-02"
    connection = postgres_engine.connect()
    connection.execute(
        text("SELECT pg_advisory_lock(:key)"),
        {"key": lock_key(value)},
    )
    connection.close()

    lock = PostgresAdvisoryJobLock(postgres_engine)
    try:
        assert lock.try_acquire(value) is True
    finally:
        lock.release(value)


def test_repeated_release_never_raises(postgres_engine: Engine) -> None:
    lock = PostgresAdvisoryJobLock(postgres_engine)
    value = "sync-daily:2026-09-02"

    assert lock.try_acquire(value) is True
    lock.release(value)
    lock.release(value)


def test_actual_macro_job_cli_and_scheduler_share_publication_lock_without_reentry(postgres_engine: Engine) -> None:
    from contextlib import contextmanager
    from dataclasses import replace
    from datetime import timedelta
    from app.config import Settings
    from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator
    from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
    from app.jobs.scheduler_state import SchedulerStateRepository
    from backend.tests.jobs.test_macro_sync import FakeProvider, result, setup_job
    provider = FakeProvider("fred", result("fred"))
    sqlite_engine, job, opts, observations, _, publisher, history, _ = setup_job((provider,))
    job._lock = PostgresAdvisoryJobLock(postgres_engine)
    contender = PostgresAdvisoryJobLock(postgres_engine)
    @contextmanager
    def runtime():
        yield MacroSyncRuntime(job, job._catalog)
    coordinator = MacroScheduledCoordinator(settings=Settings(), catalog=job._catalog,
        history=history, state=SchedulerStateRepository(history._session_factory),
        runtime_factory=runtime, publication_lock=PostgresAdvisoryJobLock(postgres_engine), clock=lambda: opts.as_of)
    assert contender.try_acquire(data_publication_lock_key())
    try:
        for offset in (0, 7):
            assert job.run(replace(opts, as_of=opts.as_of + timedelta(days=offset))).status == "locked"
        assert coordinator.run_scheduled(opts.as_of).status == "locked"
        assert provider.calls == observations.persisted == publisher.calls == []
    finally:
        contender.release(data_publication_lock_key())
    try:
        assert coordinator.run_scheduled(opts.as_of + timedelta(days=7)).status == "partial"
        assert provider.calls and publisher.calls
        assert contender.try_acquire(data_publication_lock_key())
    finally:
        contender.release(data_publication_lock_key())
        sqlite_engine.dispose()
