from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.contracts import Attempt, Evidence, StateKey, utc
from app.acquisition.repository import KEY_FIELDS, StateRepository
from app.db.models import DataAcquisitionState, JobRun


def start_attempt(session: Session, attempt: Attempt) -> bool:
    return StateRepository(session).begin(attempt)


def finish_attempt(session: Session, attempt: Attempt, evidence: Evidence) -> bool:
    return StateRepository(session).finish(attempt, evidence)


def record_failed_attempt(factory, attempt, *, code, reason, checked_at, references):
    with factory.begin() as session:
        if start_attempt(session, attempt):
            finish_attempt(session, attempt, Evidence('failed', checked_at, None, code, reason,
                                                      attempt.scope, references))


def record_terminal(session, key, *, started_at, checked_at, status, scope, references=(), code=None,
                    reason='', success_at=None, attempt_id=None, trigger='collector'):
    attempt = Attempt(key, attempt_id or str(uuid4()), started_at, scope, trigger)
    evidence = Evidence(status, checked_at, checked_at if status == 'success' else success_at,
                        code, reason, scope, references)
    return start_attempt(session, attempt) and finish_attempt(session, attempt, evidence)


def recover_attempts(factory, *, now: datetime, active_attempt_ids: set[str]) -> dict:
    recovered = 0
    with factory.begin() as session:
        rows = session.scalars(select(DataAcquisitionState).where(DataAcquisitionState.active.is_(True)))
        for row in rows:
            parent_id = row.scope.get('runId')
            if not parent_id or row.attempt_id in active_attempt_ids:
                continue
            run = session.get(JobRun, parent_id)
            if not run or run.status == 'running' or not run.finished_at or utc(run.finished_at) > now:
                continue
            key = StateKey(*(getattr(row, f) for f in KEY_FIELDS))
            attempt = Attempt(key, row.attempt_id, utc(row.started_at), row.scope, row.trigger)
            evidence = Evidence('unrecorded', now, reason='父任务已结束，未找到该次尝试的完成凭证', scope=row.scope)
            recovered += finish_attempt(session, attempt, evidence)
    return {'recovered':recovered}
