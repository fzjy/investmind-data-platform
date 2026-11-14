from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.db.models import JobFailure


@dataclass(frozen=True)
class FailureQuery:
    job_run_id: str | None = None
    datasets: tuple[Dataset, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class StoredFailure:
    id: int
    job_run_id: str | None
    dataset: Dataset
    request_key: str
    error_code: str
    message: str
    retryable: bool
    attempts: int
    request_payload: dict[str, Any]


class FailureStore(Protocol):
    def list_unresolved(self, query: FailureQuery) -> tuple[StoredFailure, ...]: ...

    def mark_resolved(self, failure_id: int) -> int: ...

    def mark_failed(
        self,
        failure_id: int,
        *,
        error_code: str,
        message: str,
        retryable: bool,
    ) -> int: ...


@dataclass(frozen=True)
class RetryOutcome:
    failure_id: int
    job_run_id: str | None
    dataset: Dataset
    request_key: str
    status: Literal["completed", "failed", "skipped"]
    attempts: int
    error_code: str | None
    message: str | None
    retryable: bool
    result: Any | None = None


@dataclass(frozen=True)
class RetryReport:
    selected: int
    attempted: int
    completed: int
    failed: int
    skipped: int
    outcomes: tuple[RetryOutcome, ...]


class RetryFailuresJob:
    def __init__(
        self,
        runner: Callable[[RequestPlan, TushareMode], Any],
        store: FailureStore,
        *,
        sanitize_error: Callable[[str], str] | None = None,
        before_retry: Callable[[], None] | None = None,
    ) -> None:
        self._runner = runner
        self._store = store
        self._sanitize_error = sanitize_error or (lambda message: message)
        self._before_retry = before_retry or (lambda: None)

    def run(self, query: FailureQuery) -> RetryReport:
        failures = self._store.list_unresolved(query)
        outcomes: list[RetryOutcome] = []
        attempted = 0
        completed = 0
        failed = 0
        skipped = 0
        prepared = False
        for failure in failures:
            if not failure.retryable:
                skipped += 1
                outcomes.append(
                    self._outcome(
                        failure,
                        status="skipped",
                        attempts=failure.attempts,
                        error_code=failure.error_code,
                        message=failure.message,
                        retryable=False,
                    )
                )
                continue
            try:
                plan, mode = deserialize_replay_request(failure.request_payload)
                if plan.dataset is not failure.dataset or plan.cursor_key != failure.request_key:
                    raise ValueError("replay request does not match the stored failure")
            except (KeyError, TypeError, ValueError) as exc:
                skipped += 1
                outcomes.append(
                    self._outcome(
                        failure,
                        status="skipped",
                        attempts=failure.attempts,
                        error_code="InvalidReplayRequest",
                        message=self._sanitize_error(str(exc)),
                        retryable=False,
                    )
                )
                continue

            if not prepared:
                self._before_retry()
                prepared = True
            attempted += 1
            try:
                result = self._runner(plan, mode)
            except Exception as exc:
                error_code = str(getattr(exc, "error_code", type(exc).__name__))
                message = self._sanitize_error(str(exc))
                retryable = bool(getattr(exc, "retryable", False))
                attempts = self._store.mark_failed(
                    failure.id,
                    error_code=error_code,
                    message=message,
                    retryable=retryable,
                )
                failed += 1
                outcomes.append(
                    self._outcome(
                        failure,
                        status="failed",
                        attempts=attempts,
                        error_code=error_code,
                        message=message,
                        retryable=retryable,
                        result=getattr(exc, "partial_result", None),
                    )
                )
                continue

            if not bool(getattr(result, "completed", True)):
                rejected_count = int(getattr(result, "rejected_count", 0))
                error_code = "RejectedRows"
                message = f"{rejected_count} row(s) rejected"
                attempts = self._store.mark_failed(
                    failure.id,
                    error_code=error_code,
                    message=message,
                    retryable=False,
                )
                failed += 1
                outcomes.append(
                    self._outcome(
                        failure,
                        status="failed",
                        attempts=attempts,
                        error_code=error_code,
                        message=message,
                        retryable=False,
                        result=result,
                    )
                )
                continue

            attempts = self._store.mark_resolved(failure.id)
            completed += 1
            outcomes.append(
                self._outcome(
                    failure,
                    status="completed",
                    attempts=attempts,
                    error_code=None,
                    message=None,
                    retryable=False,
                    result=result,
                )
            )
        return RetryReport(
            selected=len(failures),
            attempted=attempted,
            completed=completed,
            failed=failed,
            skipped=skipped,
            outcomes=tuple(outcomes),
        )

    @staticmethod
    def _outcome(
        failure: StoredFailure,
        *,
        status: Literal["completed", "failed", "skipped"],
        attempts: int,
        error_code: str | None,
        message: str | None,
        retryable: bool,
        result: Any | None = None,
    ) -> RetryOutcome:
        return RetryOutcome(
            failure_id=failure.id,
            job_run_id=failure.job_run_id,
            dataset=failure.dataset,
            request_key=failure.request_key,
            status=status,
            attempts=attempts,
            error_code=error_code,
            message=message,
            retryable=retryable,
            result=result,
        )


class SqlFailureRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_unresolved(self, query: FailureQuery) -> tuple[StoredFailure, ...]:
        statement = select(JobFailure).where(JobFailure.resolved_at.is_(None))
        if query.job_run_id is not None:
            statement = statement.where(JobFailure.job_run_id == query.job_run_id)
        if query.datasets:
            statement = statement.where(
                JobFailure.dataset.in_(dataset.value for dataset in query.datasets)
            )
        if query.error_code is not None:
            statement = statement.where(JobFailure.error_code == query.error_code)
        statement = statement.order_by(JobFailure.id)
        with self._session_factory() as session:
            failures = tuple(session.scalars(statement))
        return tuple(
            StoredFailure(
                id=failure.id,
                job_run_id=failure.job_run_id,
                dataset=Dataset(failure.dataset),
                request_key=failure.request_key,
                error_code=failure.error_code,
                message=failure.message,
                retryable=failure.retryable,
                attempts=failure.attempts,
                request_payload=dict(failure.request_payload),
            )
            for failure in failures
        )

    def mark_resolved(self, failure_id: int) -> int:
        with self._session_factory.begin() as session:
            failure = self._require_failure(session, failure_id)
            failure.attempts += 1
            attempted_at = datetime.now(timezone.utc)
            failure.last_attempted_at = attempted_at
            failure.resolved_at = attempted_at
            return failure.attempts

    def mark_failed(
        self,
        failure_id: int,
        *,
        error_code: str,
        message: str,
        retryable: bool,
    ) -> int:
        with self._session_factory.begin() as session:
            failure = self._require_failure(session, failure_id)
            failure.attempts += 1
            failure.last_attempted_at = datetime.now(timezone.utc)
            failure.error_code = error_code
            failure.message = message
            failure.retryable = retryable
            return failure.attempts

    @staticmethod
    def _require_failure(session: Session, failure_id: int) -> JobFailure:
        failure = session.get(JobFailure, failure_id)
        if failure is None:
            raise LookupError(f"job failure not found: {failure_id}")
        return failure


def serialize_replay_request(
    plan: RequestPlan,
    mode: TushareMode,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "mode": mode.value,
        "dataset": plan.dataset.value,
        "apiName": plan.api_name,
        "params": dict(plan.params),
        "fields": list(plan.fields),
        "cursorKey": plan.cursor_key,
        "pageSize": plan.page_size,
    }


def deserialize_replay_request(
    payload: Mapping[str, Any],
) -> tuple[RequestPlan, TushareMode]:
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported replay request schema")
    try:
        mode = TushareMode(payload["mode"])
        dataset = Dataset(payload["dataset"])
        api_name = payload["apiName"]
        params = payload["params"]
        fields = payload["fields"]
        cursor_key = payload["cursorKey"]
        page_size = payload["pageSize"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid replay request payload") from exc
    if not isinstance(api_name, str) or not api_name:
        raise ValueError("invalid replay request payload")
    if not isinstance(params, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in params.items()
    ):
        raise ValueError("invalid replay request payload")
    if not isinstance(fields, list) or not fields or not all(
        isinstance(field, str) and field for field in fields
    ):
        raise ValueError("invalid replay request payload")
    if not isinstance(cursor_key, str) or not cursor_key:
        raise ValueError("invalid replay request payload")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("invalid replay request payload")
    plan = RequestPlan(
        dataset=dataset,
        api_name=api_name,
        params=dict(params),
        fields=tuple(fields),
        cursor_key=cursor_key,
        page_size=page_size,
    )
    return plan, mode
