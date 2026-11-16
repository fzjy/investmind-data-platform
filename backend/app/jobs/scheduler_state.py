from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    JobFailure,
    JobRun,
    OperationalAlert,
    SchedulerCheckpoint,
    SchedulerHeartbeat,
    TradeCalendar,
)
from app.stocks.jobs.daily_sync_history import daily_sync_business_key
from app.stocks.universes.models import UniverseSelection


@dataclass(frozen=True, slots=True)
class HeartbeatView:
    instance_id: str
    scheduler_name: str
    state: str
    started_at: datetime
    last_heartbeat_at: datetime
    next_run_at: datetime | None
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class CheckpointView:
    key: str
    managed_from: date
    created_at: datetime
    last_evaluated_at: datetime
    details: dict[str, object]

    @property
    def checkpoint_key(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class AlertView:
    id: int
    key: str
    alert_type: str
    severity: str
    status: str
    first_triggered_at: datetime
    last_triggered_at: datetime
    resolved_at: datetime | None
    details: dict[str, object]

    @property
    def alert_key(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class DailyRunView:
    id: str
    business_date: date
    status: str
    started_at: datetime
    finished_at: datetime | None
    request_count: int
    row_count: int
    error_message: str | None
    details: dict[str, object]


def _json_copy(value: dict[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(value))
    if not isinstance(copied, dict):
        raise TypeError("scheduler state details must be a JSON object")
    return copied


def _checkpoint_key(universe: UniverseSelection) -> str:
    return f"sync-daily:{universe.profile}:{universe.version}"


def _business_date_for_run(run: JobRun, universe: UniverseSelection) -> date | None:
    try:
        business_date = date.fromisoformat(run.business_key[:10])
    except ValueError:
        return None
    if run.business_key != daily_sync_business_key(business_date, universe):
        return None
    return business_date


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else float("-inf")


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _is_expected_duplicate_key(
    error: IntegrityError,
    session: Session,
    *,
    sqlite_target: str,
    postgresql_constraints: frozenset[str],
) -> bool:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        return str(error.orig) == f"UNIQUE constraint failed: {sqlite_target}"
    if dialect_name == "postgresql":
        sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig,
            "pgcode",
            None,
        )
        diagnostic = getattr(error.orig, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        return sqlstate == "23505" and constraint_name in postgresql_constraints
    return False


class SchedulerStateRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start_instance(
        self,
        scheduler_name: str,
        *,
        now: datetime,
        details: dict[str, object],
    ) -> str:
        instance_id = str(uuid4())
        with self._session_factory.begin() as session:
            session.add(
                SchedulerHeartbeat(
                    instance_id=instance_id,
                    scheduler_name=scheduler_name,
                    state="running",
                    started_at=now,
                    last_heartbeat_at=now,
                    next_run_at=None,
                    details=_json_copy(details),
                )
            )
        return instance_id

    def touch_instance(
        self,
        instance_id: str,
        *,
        now: datetime,
        next_run_at: datetime | None,
        state: str,
    ) -> HeartbeatView:
        with self._session_factory.begin() as session:
            heartbeat = self._require_heartbeat(session, instance_id)
            heartbeat.last_heartbeat_at = now
            heartbeat.next_run_at = next_run_at
            heartbeat.state = state
            return self._heartbeat_view(heartbeat)

    def stop_instance(self, instance_id: str, *, now: datetime) -> None:
        with self._session_factory.begin() as session:
            heartbeat = self._require_heartbeat(session, instance_id)
            heartbeat.state = "stopping"
            heartbeat.last_heartbeat_at = now

    def latest_instance(self, scheduler_name: str) -> HeartbeatView | None:
        statement = (
            select(SchedulerHeartbeat)
            .where(SchedulerHeartbeat.scheduler_name == scheduler_name)
            .order_by(
                SchedulerHeartbeat.last_heartbeat_at.desc(),
                SchedulerHeartbeat.started_at.desc(),
                SchedulerHeartbeat.instance_id.desc(),
            )
            .limit(1)
        )
        with self._session_factory() as session:
            heartbeat = session.scalar(statement)
            return self._heartbeat_view(heartbeat) if heartbeat is not None else None

    def heartbeat(self, instance_id: str) -> HeartbeatView | None:
        with self._session_factory() as session:
            heartbeat = session.get(SchedulerHeartbeat, instance_id)
            return self._heartbeat_view(heartbeat) if heartbeat is not None else None

    def checkpoint(self, universe: UniverseSelection) -> CheckpointView | None:
        with self._session_factory() as session:
            checkpoint = session.get(SchedulerCheckpoint, _checkpoint_key(universe))
            return self._checkpoint_view(checkpoint) if checkpoint is not None else None

    def ensure_checkpoint(
        self,
        universe: UniverseSelection,
        *,
        fallback: date,
        now: datetime,
    ) -> CheckpointView:
        key = _checkpoint_key(universe)
        with self._session_factory.begin() as session:
            checkpoint = session.get(SchedulerCheckpoint, key)
            if checkpoint is None:
                latest_completed = self._latest_daily_run_in_session(
                    session,
                    universe,
                    statuses=("completed",),
                )
                candidate = SchedulerCheckpoint(
                    checkpoint_key=key,
                    managed_from=(
                        latest_completed.business_date
                        if latest_completed is not None
                        else fallback
                    ),
                    created_at=now,
                    last_evaluated_at=now,
                    details={},
                )
                try:
                    with session.begin_nested():
                        session.add(candidate)
                        session.flush()
                except IntegrityError as error:
                    if not _is_expected_duplicate_key(
                        error,
                        session,
                        sqlite_target="scheduler_checkpoint.checkpoint_key",
                        postgresql_constraints=frozenset(
                            {
                                "pk_scheduler_checkpoint",
                                "scheduler_checkpoint_pkey",
                            }
                        ),
                    ):
                        raise
                    checkpoint = session.get(SchedulerCheckpoint, key)
                    if checkpoint is None:
                        raise
                else:
                    checkpoint = candidate
            return self._checkpoint_view(checkpoint)

    def update_checkpoint_evaluation(
        self,
        key: str,
        *,
        now: datetime,
        details: dict[str, object],
    ) -> None:
        with self._session_factory.begin() as session:
            checkpoint = session.get(SchedulerCheckpoint, key)
            if checkpoint is None:
                raise LookupError(f"scheduler checkpoint not found: {key}")
            checkpoint.last_evaluated_at = now
            checkpoint.details = _json_copy(details)

    def latest_open_date(self, *, on_or_before: date) -> date | None:
        statement = (
            select(TradeCalendar.cal_date)
            .where(
                TradeCalendar.exchange == "SSE",
                TradeCalendar.is_open.is_(True),
                TradeCalendar.cal_date <= on_or_before,
            )
            .order_by(TradeCalendar.cal_date.desc())
            .limit(1)
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def calendar_covers(self, *, start: date, end: date) -> bool:
        if start > end:
            return True
        with self._session_factory() as session:
            count = session.scalar(select(func.count()).select_from(TradeCalendar).where(
                TradeCalendar.exchange == "SSE", TradeCalendar.cal_date >= start,
                TradeCalendar.cal_date <= end,
            ))
        return count == (end - start).days + 1

    def trading_day(self, business_date: date) -> bool | None:
        statement = select(TradeCalendar.is_open).where(
            TradeCalendar.exchange == "SSE",
            TradeCalendar.cal_date == business_date,
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def open_dates(self, *, start: date, end: date) -> tuple[date, ...]:
        statement = (
            select(TradeCalendar.cal_date)
            .where(
                TradeCalendar.exchange == "SSE",
                TradeCalendar.is_open.is_(True),
                TradeCalendar.cal_date >= start,
                TradeCalendar.cal_date <= end,
            )
            .order_by(TradeCalendar.cal_date)
        )
        with self._session_factory() as session:
            return tuple(session.scalars(statement))

    def completed_dates(
        self,
        universe: UniverseSelection,
        dates: tuple[date, ...],
    ) -> frozenset[date]:
        if not dates:
            return frozenset()
        expected_keys = {
            daily_sync_business_key(business_date, universe): business_date
            for business_date in dates
        }
        statement = select(JobRun.business_key).where(
            JobRun.job_name == "sync-daily",
            JobRun.status == "completed",
            JobRun.business_key.in_(expected_keys),
        )
        with self._session_factory() as session:
            return frozenset(expected_keys[key] for key in session.scalars(statement))

    def latest_daily_run(
        self,
        universe: UniverseSelection,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> DailyRunView | None:
        with self._session_factory() as session:
            return self._latest_daily_run_in_session(
                session,
                universe,
                statuses=statuses,
            )

    def consecutive_failures(self, universe: UniverseSelection) -> int:
        with self._session_factory() as session:
            runs = self._daily_run_views_in_session(session, universe)
        consecutive = 0
        for run in reversed(runs):
            if run.status == "failed":
                consecutive += 1
            elif run.status in {"completed", "partial"}:
                break
        return consecutive

    def recover_interrupted_runs(
        self,
        universe: UniverseSelection,
        *,
        now: datetime,
        timeout: timedelta,
    ) -> tuple[str, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        statement = (
            select(JobRun)
            .where(
                JobRun.job_name == "sync-daily",
                JobRun.status == "running",
            )
            .order_by(JobRun.started_at, JobRun.id)
            .with_for_update()
        )
        recovered: list[str] = []
        with self._session_factory.begin() as session:
            for run in session.scalars(statement):
                if _business_date_for_run(run, universe) is None:
                    continue
                details = dict(run.details or {})
                instance_id = details.get("schedulerInstanceId")
                if details.get("trigger") != "scheduler" or not isinstance(
                    instance_id,
                    str,
                ):
                    continue
                heartbeat = session.get(SchedulerHeartbeat, instance_id)
                if heartbeat is None or heartbeat.scheduler_name != "daily-sync":
                    continue
                timed_out = (
                    _utc_naive(now) - _utc_naive(heartbeat.last_heartbeat_at)
                    > timeout
                )
                if heartbeat.state != "stopping" and not timed_out:
                    continue
                message = "scheduler stopped before the run finished"
                run.status = "failed"
                run.finished_at = now
                run.error_message = message
                run.details = {
                    **details,
                    "errorCode": "SCHEDULER_INTERRUPTED",
                }
                session.add(
                    JobFailure(
                        job_run_id=run.id,
                        dataset="scheduler",
                        request_key=run.business_key,
                        error_code="SCHEDULER_INTERRUPTED",
                        message=message,
                        retryable=True,
                        request_payload={},
                        last_attempted_at=now,
                    )
                )
                recovered.append(run.id)
        from app.acquisition.tracking import recover_attempts
        recover_attempts(self._session_factory,now=now,active_attempt_ids=set())
        return tuple(recovered)

    def open_alert(
        self,
        key: str,
        alert_type: str,
        severity: str,
        now: datetime,
        details: dict[str, object],
    ) -> AlertView:
        with self._session_factory.begin() as session:
            alert = session.scalar(
                select(OperationalAlert).where(OperationalAlert.alert_key == key)
            )
            if alert is None:
                candidate = OperationalAlert(
                    alert_key=key,
                    alert_type=alert_type,
                    severity=severity,
                    status="open",
                    first_triggered_at=now,
                    last_triggered_at=now,
                    resolved_at=None,
                    details=_json_copy(details),
                )
                try:
                    with session.begin_nested():
                        session.add(candidate)
                        session.flush()
                except IntegrityError as error:
                    if not _is_expected_duplicate_key(
                        error,
                        session,
                        sqlite_target="operational_alert.alert_key",
                        postgresql_constraints=frozenset(
                            {"uq_operational_alert_alert_key"}
                        ),
                    ):
                        raise
                    alert = session.scalar(
                        select(OperationalAlert).where(OperationalAlert.alert_key == key)
                    )
                    if alert is None:
                        raise
                else:
                    return self._alert_view(candidate)

            if alert.status == "open":
                alert.severity = severity
                alert.last_triggered_at = now
                alert.details = _json_copy(details)
            else:
                alert.alert_type = alert_type
                alert.severity = severity
                alert.status = "open"
                alert.first_triggered_at = now
                alert.last_triggered_at = now
                alert.resolved_at = None
                alert.details = _json_copy(details)
            return self._alert_view(alert)

    def resolve_alert(self, key: str, now: datetime) -> None:
        with self._session_factory.begin() as session:
            alert = session.scalar(
                select(OperationalAlert).where(OperationalAlert.alert_key == key)
            )
            if alert is not None and alert.status != "resolved":
                alert.status = "resolved"
                alert.resolved_at = now

    def alert(self, key: str) -> AlertView | None:
        statement = select(OperationalAlert).where(OperationalAlert.alert_key == key)
        with self._session_factory() as session:
            alert = session.scalar(statement)
            return self._alert_view(alert) if alert is not None else None

    def open_alerts(self) -> tuple[AlertView, ...]:
        statement = (
            select(OperationalAlert)
            .where(OperationalAlert.status == "open")
            .order_by(OperationalAlert.alert_key)
        )
        with self._session_factory() as session:
            return tuple(self._alert_view(alert) for alert in session.scalars(statement))

    @staticmethod
    def _require_heartbeat(session: Session, instance_id: str) -> SchedulerHeartbeat:
        heartbeat = session.get(SchedulerHeartbeat, instance_id)
        if heartbeat is None:
            raise LookupError(f"scheduler heartbeat not found: {instance_id}")
        return heartbeat

    @staticmethod
    def _heartbeat_view(heartbeat: SchedulerHeartbeat) -> HeartbeatView:
        return HeartbeatView(
            instance_id=heartbeat.instance_id,
            scheduler_name=heartbeat.scheduler_name,
            state=heartbeat.state,
            started_at=heartbeat.started_at,
            last_heartbeat_at=heartbeat.last_heartbeat_at,
            next_run_at=heartbeat.next_run_at,
            details=_json_copy(heartbeat.details),
        )

    @staticmethod
    def _checkpoint_view(checkpoint: SchedulerCheckpoint) -> CheckpointView:
        return CheckpointView(
            key=checkpoint.checkpoint_key,
            managed_from=checkpoint.managed_from,
            created_at=checkpoint.created_at,
            last_evaluated_at=checkpoint.last_evaluated_at,
            details=_json_copy(checkpoint.details),
        )

    @staticmethod
    def _alert_view(alert: OperationalAlert) -> AlertView:
        return AlertView(
            id=alert.id,
            key=alert.alert_key,
            alert_type=alert.alert_type,
            severity=alert.severity,
            status=alert.status,
            first_triggered_at=alert.first_triggered_at,
            last_triggered_at=alert.last_triggered_at,
            resolved_at=alert.resolved_at,
            details=_json_copy(alert.details),
        )

    def _latest_daily_run_in_session(
        self,
        session: Session,
        universe: UniverseSelection,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> DailyRunView | None:
        runs = self._daily_run_views_in_session(session, universe, statuses=statuses)
        return runs[-1] if runs else None

    def _daily_run_views_in_session(
        self,
        session: Session,
        universe: UniverseSelection,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> list[DailyRunView]:
        predicates = [JobRun.job_name == "sync-daily"]
        if statuses is not None:
            predicates.append(JobRun.status.in_(statuses))
        rows = session.scalars(select(JobRun).where(*predicates)).all()
        views = [
            self._daily_run_view(run, business_date)
            for run in rows
            if (business_date := _business_date_for_run(run, universe)) is not None
        ]
        return sorted(
            views,
            key=lambda run: (
                _timestamp(run.finished_at or run.started_at),
                _timestamp(run.started_at),
                run.business_date,
                run.id,
            ),
        )

    @staticmethod
    def _daily_run_view(run: JobRun, business_date: date) -> DailyRunView:
        return DailyRunView(
            id=run.id,
            business_date=business_date,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            request_count=run.request_count,
            row_count=run.row_count,
            error_message=run.error_message,
            details=_json_copy(run.details),
        )
