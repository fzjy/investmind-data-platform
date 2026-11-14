from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.db.base import Base
from app.db.models import JobFailure, JobRun
from app.jobs.failure_retries import (
    FailureQuery,
    RetryFailuresJob,
    SqlFailureRepository,
    StoredFailure,
    deserialize_replay_request,
    serialize_replay_request,
)


def test_replay_request_round_trips_every_request_plan_field() -> None:
    plan = RequestPlan(
        dataset=Dataset.INCOME,
        api_name="income_vip",
        params={"period": "20251231", "offset": "5000"},
        fields=("ts_code", "ann_date", "revenue"),
        cursor_key="income:period:20251231",
        page_size=1000,
    )

    payload = serialize_replay_request(plan, TushareMode.VIP)
    restored_plan, restored_mode = deserialize_replay_request(payload)

    assert payload == {
        "schemaVersion": 1,
        "mode": "vip",
        "dataset": "income",
        "apiName": "income_vip",
        "params": {"period": "20251231", "offset": "5000"},
        "fields": ["ts_code", "ann_date", "revenue"],
        "cursorKey": "income:period:20251231",
        "pageSize": 1000,
    }
    assert restored_plan == plan
    assert restored_mode is TushareMode.VIP


def _sqlite_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
                "derived": None,
                "mart": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def _daily_payload() -> dict[str, object]:
    return serialize_replay_request(
        RequestPlan(
            dataset=Dataset.DAILY,
            api_name="daily",
            params={
                "ts_code": "600519.SH",
                "start_date": "20230817",
                "end_date": "20260817",
            },
            fields=("ts_code", "trade_date", "close"),
            cursor_key="daily:600519.SH",
        ),
        TushareMode.STANDARD,
    )


def test_sql_failure_repository_filters_unresolved_failures_and_resolves_one() -> None:
    engine, factory = _sqlite_factory()
    with factory.begin() as session:
        session.add_all(
            [
                JobRun(id="run-1", job_name="backfill", business_key="one", status="failed"),
                JobRun(id="run-2", job_name="backfill", business_key="two", status="failed"),
            ]
        )
        session.add_all(
            [
                JobFailure(
                    job_run_id="run-1",
                    dataset="daily",
                    request_key="daily:600519.SH",
                    error_code="TushareTransientError",
                    message="temporary outage",
                    retryable=True,
                    request_payload=_daily_payload(),
                ),
                JobFailure(
                    job_run_id="run-2",
                    dataset="income",
                    request_key="income:000001.SZ",
                    error_code="TusharePermissionError",
                    message="permission denied",
                    retryable=False,
                    request_payload={},
                ),
            ]
        )

    repository = SqlFailureRepository(factory)
    matches = repository.list_unresolved(
        FailureQuery(
            job_run_id="run-1",
            datasets=(Dataset.DAILY,),
            error_code="TushareTransientError",
        )
    )

    assert len(matches) == 1
    assert matches[0].job_run_id == "run-1"
    assert matches[0].dataset is Dataset.DAILY
    assert matches[0].request_key == "daily:600519.SH"
    assert matches[0].attempts == 1
    assert matches[0].request_payload == _daily_payload()

    attempts = repository.mark_resolved(matches[0].id)

    assert attempts == 2
    assert repository.list_unresolved(
        FailureQuery(
            job_run_id="run-1",
            datasets=(Dataset.DAILY,),
            error_code="TushareTransientError",
        )
    ) == ()
    remaining = repository.list_unresolved(FailureQuery())
    assert len(remaining) == 1
    assert remaining[0].job_run_id == "run-2"
    assert remaining[0].retryable is False
    with factory() as session:
        stored = session.scalar(select(JobFailure).where(JobFailure.id == matches[0].id))
    assert stored is not None
    assert stored.attempts == 2
    assert stored.last_attempted_at is not None
    assert stored.resolved_at is not None
    engine.dispose()


class MemoryFailureStore:
    def __init__(self, failures: tuple[StoredFailure, ...]) -> None:
        self.failures = failures
        self.resolved: list[int] = []
        self.failed: list[tuple[int, str, str, bool]] = []

    def list_unresolved(self, _query: FailureQuery) -> tuple[StoredFailure, ...]:
        return self.failures

    def mark_resolved(self, failure_id: int) -> int:
        self.resolved.append(failure_id)
        return 2

    def mark_failed(
        self,
        failure_id: int,
        *,
        error_code: str,
        message: str,
        retryable: bool,
    ) -> int:
        self.failed.append((failure_id, error_code, message, retryable))
        return 2


def _stored_failure(
    failure_id: int,
    *,
    retryable: bool = True,
    request_payload: dict[str, object] | None = None,
) -> StoredFailure:
    return StoredFailure(
        id=failure_id,
        job_run_id="run-1",
        dataset=Dataset.DAILY,
        request_key="daily:600519.SH",
        error_code="TushareTransientError",
        message="temporary outage",
        retryable=retryable,
        attempts=1,
        request_payload=_daily_payload() if request_payload is None else request_payload,
    )


def test_retry_job_replays_retryable_requests_and_preserves_unretryable_failures() -> None:
    store = MemoryFailureStore(
        (
            _stored_failure(1),
            _stored_failure(2, retryable=False),
            _stored_failure(3, request_payload={}),
        )
    )
    calls: list[tuple[RequestPlan, TushareMode]] = []

    def run(plan: RequestPlan, mode: TushareMode):
        calls.append((plan, mode))
        return SimpleNamespace(completed=True, rejected_count=0)

    report = RetryFailuresJob(run, store).run(FailureQuery())

    assert report.selected == 3
    assert report.attempted == 1
    assert report.completed == 1
    assert report.failed == 0
    assert report.skipped == 2
    assert [outcome.status for outcome in report.outcomes] == [
        "completed",
        "skipped",
        "skipped",
    ]
    assert report.outcomes[1].error_code == "TushareTransientError"
    assert report.outcomes[2].error_code == "InvalidReplayRequest"
    assert store.resolved == [1]
    assert store.failed == []
    assert len(calls) == 1
    assert calls[0][0].params == {
        "ts_code": "600519.SH",
        "start_date": "20230817",
        "end_date": "20260817",
    }
    assert calls[0][1] is TushareMode.STANDARD


def test_retry_job_updates_the_original_failure_when_retry_fails() -> None:
    store = MemoryFailureStore((_stored_failure(1),))

    class RetryableProviderError(RuntimeError):
        retryable = True

    def fail(_plan: RequestPlan, _mode: TushareMode):
        raise RetryableProviderError("provider rejected token secret-token")

    report = RetryFailuresJob(
        fail,
        store,
        sanitize_error=lambda message: message.replace("secret-token", "***"),
    ).run(FailureQuery())

    assert report.selected == 1
    assert report.attempted == 1
    assert report.completed == 0
    assert report.failed == 1
    assert report.skipped == 0
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].attempts == 2
    assert report.outcomes[0].error_code == "RetryableProviderError"
    assert report.outcomes[0].message == "provider rejected token ***"
    assert report.outcomes[0].retryable is True
    assert store.resolved == []
    assert store.failed == [
        (1, "RetryableProviderError", "provider rejected token ***", True)
    ]


def test_retry_job_does_not_resolve_a_request_with_rejected_rows() -> None:
    store = MemoryFailureStore((_stored_failure(1),))

    report = RetryFailuresJob(
        lambda _plan, _mode: SimpleNamespace(completed=False, rejected_count=2),
        store,
    ).run(FailureQuery())

    assert report.attempted == 1
    assert report.completed == 0
    assert report.failed == 1
    assert report.skipped == 0
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].error_code == "RejectedRows"
    assert report.outcomes[0].message == "2 row(s) rejected"
    assert report.outcomes[0].retryable is False
    assert store.resolved == []
    assert store.failed == [(1, "RejectedRows", "2 row(s) rejected", False)]


def test_deserialize_replay_request_rejects_an_unsupported_schema() -> None:
    with pytest.raises(ValueError, match="unsupported replay request schema"):
        deserialize_replay_request({"schemaVersion": 2})


def test_sql_failure_repository_records_a_failed_retry_without_resolving_it() -> None:
    engine, factory = _sqlite_factory()
    with factory.begin() as session:
        session.add(JobRun(id="run-1", job_name="backfill", business_key="one", status="failed"))
        failure = JobFailure(
            job_run_id="run-1",
            dataset="daily",
            request_key="daily:600519.SH",
            error_code="TushareTransientError",
            message="first outage",
            retryable=True,
            request_payload=_daily_payload(),
        )
        session.add(failure)

    repository = SqlFailureRepository(factory)
    stored = repository.list_unresolved(FailureQuery())[0]
    attempts = repository.mark_failed(
        stored.id,
        error_code="TushareAuthError",
        message="token *** expired",
        retryable=False,
    )

    assert attempts == 2
    with factory() as session:
        updated = session.get(JobFailure, stored.id)
    assert updated is not None
    assert updated.error_code == "TushareAuthError"
    assert updated.message == "token *** expired"
    assert updated.retryable is False
    assert updated.attempts == 2
    assert updated.last_attempted_at is not None
    assert updated.resolved_at is None
    engine.dispose()
