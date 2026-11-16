from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Engine

from app.connectors.tushare.strategies import TushareMode
from app.stocks.jobs.backfill import BackfillReport
from app.jobs.build_and_publish import BuildAndPublishReport
from app.jobs.locks import (
    PostgresAdvisoryJobLock,
    ProcessLocalJobLock,
    daily_sync_lock_key,
    data_publication_lock_key,
    job_lock_for_engine,
    lock_key,
)
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillCounts,
    UniverseBackfillJob,
    UniverseBackfillOptions,
    UniverseBackfillPlanner,
)
from app.stocks.universes.models import UniverseSelection


class FakeConnection:
    def __init__(
        self,
        *,
        scalar_result: bool = True,
        scalar_error: Exception | None = None,
        execute_error: Exception | None = None,
    ) -> None:
        self.scalar_result = scalar_result
        self.scalar_error = scalar_error
        self.execute_error = execute_error
        self.close_calls = 0

    def scalar(self, *_args: object, **_kwargs: object) -> bool:
        if self.scalar_error is not None:
            raise self.scalar_error
        return self.scalar_result

    def execute(self, *_args: object, **_kwargs: object) -> None:
        if self.execute_error is not None:
            raise self.execute_error

    def close(self) -> None:
        self.close_calls += 1


def postgres_lock_with(connection: FakeConnection) -> PostgresAdvisoryJobLock:
    engine = cast(
        Engine,
        SimpleNamespace(connect=lambda: connection),
    )
    return PostgresAdvisoryJobLock(engine)


def test_lock_key_preserves_sha256_signed_big_endian_semantics() -> None:
    assert lock_key("sync-daily:2026-09-02") == -2974280154852157027


def test_daily_lock_key_preserves_daily_namespace() -> None:
    value = daily_sync_lock_key(date(2026, 9, 2))

    assert value == "sync-daily:2026-09-02"
    assert lock_key(value) == -2974280154852157027


def test_publication_lock_is_global_while_daily_run_locks_remain_date_scoped() -> None:
    first = ProcessLocalJobLock()
    second = ProcessLocalJobLock()
    first_date = daily_sync_lock_key(date(2026, 9, 2))
    next_date = daily_sync_lock_key(date(2026, 9, 3))
    publication = data_publication_lock_key()

    assert first_date != next_date
    assert first.try_acquire(first_date) is True
    assert second.try_acquire(next_date) is True
    assert first.try_acquire(publication) is True
    try:
        assert second.try_acquire(publication) is False
    finally:
        first.release(first_date)
        second.release(next_date)
        first.release(publication)


def test_universe_backfill_holds_publication_lock_only_at_publish_boundary() -> None:
    events: list[tuple[str, str]] = []
    selection = UniverseSelection(
        profile="custom",
        version="custom-v1",
        coverage="custom",
        symbols=("600519.SH",),
        fingerprint="custom-fingerprint",
    )
    options = UniverseBackfillOptions(
        as_of=date(2026, 9, 2),
        mode=TushareMode.STANDARD,
        selection=selection,
    )
    publication_key = data_publication_lock_key()

    class RecordingLock:
        def __init__(self, name: str) -> None:
            self.name = name
            self.held = False

        def try_acquire(self, value: str) -> bool:
            events.append((f"{self.name}.acquire", value))
            self.held = True
            return True

        def release(self, value: str) -> None:
            events.append((f"{self.name}.release", value))
            self.held = False

    run_lock = RecordingLock("run")
    publication_lock = RecordingLock("publication")

    class History:
        def begin(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                action="start",
                run_id="run-1",
                cursor_job_name="backfill-universe:run-1",
                resumed=False,
                global_completed=False,
                next_batch_index=0,
                counts=UniverseBackfillCounts(),
            )

        def checkpoint(self, *_args: object, **_kwargs: object) -> None:
            pass

        def finish(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fail(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("successful run must not fail history")

    class Runner:
        def run(self, *_args: object, **_kwargs: object) -> BackfillReport:
            assert run_lock.held is True
            assert publication_lock.held is False
            events.append(("batch.run", "without-publication-lock"))
            return BackfillReport(1, 1, 0, ())

    class Publisher:
        def run(self, publish_options: object) -> BuildAndPublishReport:
            assert run_lock.held is True
            assert publication_lock.held is True
            events.append(("publish", "with-publication-lock"))
            return BuildAndPublishReport(
                status="completed",
                job_run_id="publish-run",
                as_of=options.as_of,
                version="snapshot-v1",
                symbols=selection.symbols,
                metric_count=0,
                risk_count=0,
                products=(),
                quality=(),
                missing_data=(),
            )

    job = UniverseBackfillJob(
        planner=UniverseBackfillPlanner(),
        history=History(),
        batch_runner_factory=lambda _job_name: Runner(),
        publisher=Publisher(),
        run_lock=run_lock,
        publication_lock=publication_lock,
        clock=lambda: datetime(2026, 9, 3, tzinfo=UTC),
        sanitize_error=lambda message: message,
    )

    report = job.run(options)

    assert report.status == "completed"
    assert events.count(("batch.run", "without-publication-lock")) == 2
    assert ("publication.acquire", publication_key) in events
    assert events.index(("publication.acquire", publication_key)) > max(
        index
        for index, event in enumerate(events)
        if event == ("batch.run", "without-publication-lock")
    )
    assert events[-2:] == [
        ("publication.release", publication_key),
        ("run.release", f"backfill-universe:{report.plan.business_key}"),
    ]


def test_second_worker_skips_same_process_lock() -> None:
    first = ProcessLocalJobLock()
    second = ProcessLocalJobLock()

    assert first.try_acquire("daily:20260814") is True
    assert second.try_acquire("daily:20260814") is False

    first.release("daily:20260814")
    assert second.try_acquire("daily:20260814") is True
    second.release("daily:20260814")


def test_process_local_lock_allows_different_keys() -> None:
    first = ProcessLocalJobLock()
    second = ProcessLocalJobLock()

    assert first.try_acquire("sync-daily:2026-09-02") is True
    assert second.try_acquire("sync-daily:2026-09-03") is True

    first.release("sync-daily:2026-09-02")
    second.release("sync-daily:2026-09-03")


def test_process_local_lock_is_idempotent_and_does_not_reenter() -> None:
    lock = ProcessLocalJobLock()

    assert lock.try_acquire("sync-daily:2026-09-02") is True
    assert lock.try_acquire("sync-daily:2026-09-02") is False
    lock.release("sync-daily:2026-09-02")
    lock.release("sync-daily:2026-09-02")
    assert lock.try_acquire("sync-daily:2026-09-02") is True
    lock.release("sync-daily:2026-09-02")


def test_job_lock_factory_uses_postgres_only_for_postgresql_dialect(pg_factory) -> None:
    assert isinstance(job_lock_for_engine(pg_factory.kw["bind"]), PostgresAdvisoryJobLock)


def test_postgres_lock_closes_connection_when_not_acquired() -> None:
    connection = FakeConnection(scalar_result=False)
    lock = postgres_lock_with(connection)

    assert lock.try_acquire("sync-daily:2026-09-02") is False
    assert connection.close_calls == 1


def test_postgres_lock_closes_connection_when_acquire_query_raises() -> None:
    connection = FakeConnection(scalar_error=RuntimeError("acquire failed"))
    lock = postgres_lock_with(connection)

    with pytest.raises(RuntimeError, match="acquire failed"):
        lock.try_acquire("sync-daily:2026-09-02")

    assert connection.close_calls == 1


def test_postgres_lock_closes_connection_when_unlock_query_raises() -> None:
    connection = FakeConnection(execute_error=RuntimeError("unlock failed"))
    lock = postgres_lock_with(connection)
    value = "sync-daily:2026-09-02"

    assert lock.try_acquire(value) is True
    with pytest.raises(RuntimeError, match="unlock failed"):
        lock.release(value)

    assert connection.close_calls == 1
    lock.release(value)
    assert connection.close_calls == 1
