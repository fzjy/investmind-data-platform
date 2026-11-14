from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Literal, Protocol
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.strategies import TushareMode
from app.db.models import JobFailure, JobRun
from app.stocks.jobs.backfill import PlanFailure
from app.stocks.universes.models import UniverseSelection


DailySyncStatus = Literal["completed", "partial", "failed", "skipped", "locked"]


@dataclass(frozen=True, slots=True)
class DailySyncExecutionContext:
    trigger: Literal["cli", "scheduler"] = "cli"
    scheduler_instance_id: str | None = None

    def __post_init__(self) -> None:
        if self.trigger == "scheduler" and not self.scheduler_instance_id:
            raise ValueError("scheduler execution requires scheduler_instance_id")

    def to_details(self) -> dict[str, object]:
        details: dict[str, object] = {"trigger": self.trigger}
        if self.scheduler_instance_id:
            details["schedulerInstanceId"] = self.scheduler_instance_id
        return details


@dataclass(frozen=True)
class DailySyncFinish:
    status: DailySyncStatus
    request_count: int
    row_count: int
    details: dict[str, object]
    error_message: str | None = None


class DailySyncHistory(Protocol):
    def start(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection | None = None,
        *,
        execution: DailySyncExecutionContext = DailySyncExecutionContext(),
    ) -> str: ...

    def has_completed(
        self,
        business_date: date,
        universe: UniverseSelection | None = None,
    ) -> bool: ...

    def record_failure(self, run_id: str, failure: PlanFailure) -> None: ...

    def finish(self, run_id: str, finish: DailySyncFinish) -> None: ...

    def record_terminal(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection | None = None,
        *,
        status: Literal["skipped", "locked"],
        details: dict[str, object],
        execution: DailySyncExecutionContext = DailySyncExecutionContext(),
    ) -> str: ...


def _json_safe_copy(details: dict[str, object]) -> dict[str, object]:
    value = json.loads(json.dumps(details))
    if not isinstance(value, dict):
        raise TypeError("daily sync details must be a JSON object")
    return value


def daily_sync_business_key(
    business_date: date,
    universe: UniverseSelection | None = None,
) -> str:
    if universe is None:
        return business_date.isoformat()
    return f"{business_date.isoformat()}:{universe.profile}:{universe.version}"


class SqlDailySyncHistory:
    """Persist daily-sync history independently from ingestion batches."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection | None = None,
        *,
        execution: DailySyncExecutionContext = DailySyncExecutionContext(),
    ) -> str:
        del mode
        run_id = str(uuid4())
        details = execution.to_details()
        if universe is not None:
            details["universe"] = universe.to_payload()
        with self._session_factory.begin() as session:
            session.add(
                JobRun(
                    id=run_id,
                    job_name="sync-daily",
                    business_key=daily_sync_business_key(business_date, universe),
                    status="running",
                    details=_json_safe_copy(details),
                )
            )
        return run_id

    def has_completed(
        self,
        business_date: date,
        universe: UniverseSelection | None = None,
    ) -> bool:
        statement = select(
            exists().where(
                JobRun.job_name == "sync-daily",
                JobRun.business_key == daily_sync_business_key(business_date, universe),
                JobRun.status == "completed",
            )
        )
        with self._session_factory() as session:
            return bool(session.scalar(statement))

    def record_failure(self, run_id: str, failure: PlanFailure) -> None:
        with self._session_factory.begin() as session:
            session.add(
                JobFailure(
                    job_run_id=run_id,
                    dataset=failure.dataset.value,
                    request_key=failure.request_key,
                    error_code=failure.error_code,
                    message=failure.message,
                    retryable=failure.retryable,
                    request_payload=dict(failure.request_payload or {}),
                )
            )

    def finish(self, run_id: str, finish: DailySyncFinish) -> None:
        details = _json_safe_copy(finish.details)
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise LookupError(f"daily sync job run not found: {run_id}")
            run.status = finish.status
            run.request_count = finish.request_count
            run.row_count = finish.row_count
            run.details = {
                **details,
                **_json_safe_copy(dict(run.details or {})),
            }
            run.error_message = finish.error_message
            run.finished_at = datetime.now(timezone.utc)

    def record_terminal(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection | None = None,
        *,
        status: Literal["skipped", "locked"],
        details: dict[str, object],
        execution: DailySyncExecutionContext = DailySyncExecutionContext(),
    ) -> str:
        run_id = str(uuid4())
        terminal_details = {**details, "mode": mode.value}
        if universe is not None:
            terminal_details["universe"] = universe.to_payload()
        terminal_details.update(execution.to_details())
        terminal_details = _json_safe_copy(terminal_details)
        with self._session_factory.begin() as session:
            session.add(
                JobRun(
                    id=run_id,
                    job_name="sync-daily",
                    business_key=daily_sync_business_key(business_date, universe),
                    status=status,
                    finished_at=datetime.now(timezone.utc),
                    details=terminal_details,
                )
            )
        return run_id
