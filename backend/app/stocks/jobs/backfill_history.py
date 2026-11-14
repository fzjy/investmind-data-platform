from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobFailure, JobRun
from app.stocks.jobs.backfill import BackfillOptions, BackfillReport, PlanFailure
from app.connectors.tushare.strategies import TushareMode


class SqlBackfillHistory:
    """Persist backfill execution summaries independently from ingestion batches."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(self, options: BackfillOptions, mode: TushareMode) -> str:
        run_id = str(uuid4())
        with self._session_factory.begin() as session:
            session.add(
                JobRun(
                    id=run_id,
                    job_name="backfill",
                    business_key=f"{options.as_of.isoformat()}:{mode.value}",
                    status="running",
                )
            )
        return run_id

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
                    request_payload=failure.request_payload or {},
                )
            )

    def finish(self, run_id: str, report: BackfillReport) -> None:
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise LookupError(f"backfill job run not found: {run_id}")
            run.status = self._status(report)
            run.request_count = sum(
                int(getattr(result, "request_count", 1)) for result in report.results
            )
            run.row_count = sum(
                int(getattr(result, "row_count", 0)) for result in report.results
            )
            run.error_message = (
                f"{report.failed} request(s) failed" if report.failed else None
            )
            run.finished_at = datetime.now(timezone.utc)

    def fail(self, run_id: str, message: str) -> None:
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.error_message = message
            run.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _status(report: BackfillReport) -> str:
        if report.failed and report.completed == 0:
            return "failed"
        if report.failed or report.rejected:
            return "partial"
        return "completed"
