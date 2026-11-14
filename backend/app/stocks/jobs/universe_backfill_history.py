from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobRun
from app.stocks.jobs.universe_backfill import UniverseBackfillCounts, UniverseBackfillPlan


JOB_NAME = "backfill-universe"
UniverseBackfillTerminalStatus = Literal["completed", "partial", "failed"]


@dataclass(frozen=True)
class UniverseBackfillRunDecision:
    action: Literal["start", "resume", "skip"]
    run_id: str
    cursor_job_name: str
    resumed: bool
    global_completed: bool
    next_batch_index: int
    counts: UniverseBackfillCounts


class UniverseBackfillResumeError(RuntimeError):
    pass


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _copy_details(
    details: Mapping[str, object] | None,
) -> dict[str, object]:
    value = json.loads(json.dumps(dict(details or {})))
    if not isinstance(value, dict):
        raise TypeError("universe backfill details must be a JSON object")
    return value


def _counts_from_details(details: Mapping[str, object]) -> UniverseBackfillCounts:
    value = details.get("counts")
    payload = value if isinstance(value, dict) else {}
    return UniverseBackfillCounts(
        provider_count=int(payload.get("providerCount", 0)),
        retained_count=int(payload.get("retainedCount", 0)),
        filtered_count=int(payload.get("filteredCount", 0)),
        row_count=int(payload.get("rowCount", 0)),
        rejected_count=int(payload.get("rejectedCount", 0)),
        request_count=int(payload.get("requestCount", 0)),
    )


def _decision(
    run: JobRun,
    action: Literal["start", "resume", "skip"],
    details: Mapping[str, object],
) -> UniverseBackfillRunDecision:
    return UniverseBackfillRunDecision(
        action=action,
        run_id=run.id,
        cursor_job_name=f"{JOB_NAME}:{run.id}",
        resumed=action == "resume",
        global_completed=details.get("globalCompleted") is True,
        next_batch_index=int(details.get("nextBatchIndex", 0)),
        counts=_counts_from_details(details),
    )


def _plan_details(
    plan: UniverseBackfillPlan,
    run_id: str,
) -> dict[str, object]:
    selection = plan.options.selection
    return {
        "asOf": plan.options.as_of.isoformat(),
        "mode": plan.options.mode.value,
        "marketStart": plan.market_start.isoformat(),
        "financialStart": plan.financial_start.isoformat(),
        "batchSize": plan.options.batch_size,
        "batchCount": len(plan.symbol_batches),
        "configurationFingerprint": plan.configuration_fingerprint,
        "selectionFingerprint": selection.fingerprint,
        "universeProfile": selection.profile,
        "universeVersion": selection.version,
        "coverage": selection.coverage,
        "universeSize": len(selection.symbols),
        "cursorJobName": f"{JOB_NAME}:{run_id}",
        "globalCompleted": False,
        "nextBatchIndex": 0,
        "completedBatchCount": 0,
        "currentDataset": None,
        "counts": UniverseBackfillCounts().to_payload(),
        "attempt": 1,
    }


class SqlUniverseBackfillHistory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def begin(
        self,
        plan: UniverseBackfillPlan,
        now: datetime,
        *,
        force: bool,
    ) -> UniverseBackfillRunDecision:
        _require_aware(now)
        with self._session_factory.begin() as session:
            latest = session.scalar(
                select(JobRun)
                .where(
                    JobRun.job_name == JOB_NAME,
                    JobRun.business_key == plan.business_key,
                )
                .order_by(JobRun.started_at.desc(), JobRun.id.desc())
                .limit(1)
            )
            if latest is not None and not force:
                details = _copy_details(latest.details)
                if latest.status == "completed":
                    return _decision(latest, "skip", details)
                if (
                    details.get("configurationFingerprint")
                    != plan.configuration_fingerprint
                ):
                    raise UniverseBackfillResumeError(
                        "universe backfill configuration fingerprint mismatch; "
                        "use force to start a new run"
                    )
                latest.status = "running"
                latest.finished_at = None
                latest.error_message = None
                latest.details = {
                    **details,
                    "attempt": int(details.get("attempt", 1)) + 1,
                }
                return _decision(latest, "resume", _copy_details(latest.details))

            run_id = str(uuid4())
            if latest is not None:
                previous_details = _copy_details(latest.details)
                latest.details = {
                    **previous_details,
                    "supersededByForce": True,
                }
                if latest.status == "running":
                    latest.status = "failed"
                    latest.finished_at = now

            details = _plan_details(plan, run_id)
            created = JobRun(
                id=run_id,
                job_name=JOB_NAME,
                business_key=plan.business_key,
                status="running",
                started_at=now,
                details=_copy_details(details),
            )
            session.add(created)
            return _decision(created, "start", _copy_details(details))

    def checkpoint(
        self,
        run_id: str,
        *,
        next_batch_index: int,
        global_completed: bool,
        current_dataset: str | None,
        counts: UniverseBackfillCounts,
        now: datetime,
    ) -> None:
        _require_aware(now)
        with self._session_factory.begin() as session:
            run = self._get_run(session, run_id)
            details = _copy_details(run.details)
            run.details = {
                **details,
                "cursorJobName": f"{JOB_NAME}:{run_id}",
                "globalCompleted": global_completed,
                "nextBatchIndex": next_batch_index,
                "completedBatchCount": next_batch_index,
                "currentDataset": current_dataset,
                "counts": counts.to_payload(),
                "attempt": int(details.get("attempt", 1)),
            }
            run.request_count = counts.request_count
            run.row_count = counts.row_count

    def finish(
        self,
        run_id: str,
        status: UniverseBackfillTerminalStatus,
        now: datetime,
        *,
        details: Mapping[str, object],
    ) -> None:
        _require_aware(now)
        if status not in ("completed", "partial", "failed"):
            raise ValueError("status must be completed, partial, or failed")
        terminal_details = _copy_details(details)
        with self._session_factory.begin() as session:
            run = self._get_run(session, run_id)
            progress_details = _copy_details(run.details)
            run.status = status
            run.finished_at = now
            run.details = {
                **terminal_details,
                **progress_details,
            }

    def fail(self, run_id: str, message: str, now: datetime) -> None:
        _require_aware(now)
        with self._session_factory.begin() as session:
            run = self._get_run(session, run_id)
            run.status = "failed"
            run.error_message = message
            run.finished_at = now

    @staticmethod
    def _get_run(session: Session, run_id: str) -> JobRun:
        run = session.get(JobRun, run_id)
        if run is None or run.job_name != JOB_NAME:
            raise LookupError(f"universe backfill job run not found: {run_id}")
        return run
