from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot
from app.macro.jobs.macro_scheduler import next_macro_schedule
from app.macro.jobs.macro_sync_history import macro_safe_error_code
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.freshness import observation_expired
from app.macro.import_lineage import is_verified_import, verify_import_lineage
from app.macro.marts.macro import select_catalog_observations

from app.jobs.freshness import FreshnessService
from app.stocks.jobs.scheduler_planning import (
    CatchupPlanner,
    ExpectedBusinessDateResolver,
    TradeCalendarUnavailable,
)
from app.jobs.scheduler_state import (
    AlertView,
    DailyRunView,
    HeartbeatView,
    SchedulerStateRepository,
)
from app.stocks.universes.models import UniverseSelection


SyncStatusKind = Literal["ok", "degraded", "unavailable"]


def _disabled_macro_payload() -> dict[str, object]:
    return {
        "enabled": False, "nextRunAt": None, "lastRun": None,
        "series": {"total": 0, "completed": 0, "fresh": 0, "fallback": 0, "stale": 0, "missing": 0},
        "oldestSuccessAt": None, "snapshotVersion": None,
        "openFailures": 0, "failureSummaries": [], "degraded": False,
    }


class MacroStatusReader:
    """Read operational state and observation validity; never collect data."""

    def __init__(
        self, session_factory: sessionmaker[Session], settings: Settings, *,
        catalog: MacroSeriesCatalog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._catalog = catalog

    def read(self, now: datetime) -> dict[str, object]:
        _require_timezone_aware(now)
        if not self._settings.macro_sync_enabled:
            return _disabled_macro_payload()
        catalog = self._catalog or MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        local_now = now.astimezone(ZoneInfo(self._settings.sync_timezone))
        with self._session_factory() as session:
            states = {row.series_id: row for row in session.scalars(select(MacroSeriesState))}
            run = session.scalar(select(JobRun).where(JobRun.job_name == "sync-macro")
                                 .order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1))
            substantive = session.scalar(select(JobRun).where(
                JobRun.job_name == "sync-macro", JobRun.status.in_(("completed", "partial", "failed", "running")),
            ).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1))
            failures = session.scalars(select(JobFailure).join(JobRun, JobRun.id == JobFailure.job_run_id).where(
                JobRun.job_name == "sync-macro", JobFailure.resolved_at.is_(None),
            ).order_by(JobFailure.created_at, JobFailure.id)).all()
            snapshots = session.scalars(select(PublishedSnapshot).where(
                PublishedSnapshot.product.in_(("macro-transmission", "macro-insights")),
            )).all()
            validity_rows = session.scalars(select(MacroObservation).where(
                MacroObservation.series_id.in_(tuple(series.id for series in catalog.series)),
                MacroObservation.released_at <= now,
            )).all()
            verify_import_lineage(session, validity_rows)
        visible_latest = {
            row.series_id: row for row in select_catalog_observations(validity_rows, catalog, local_now)
        }
        latest_validity: dict[tuple[str, str, object], MacroObservation] = {}
        for row in validity_rows:
            key = (row.series_id, row.source, row.observed_at)
            prior = latest_validity.get(key)
            if prior is None or (_as_utc(row.released_at), row.revision) > (_as_utc(prior.released_at), prior.revision):
                latest_validity[key] = row
        counts = {"total": len(catalog.series), "completed": 0, "fresh": 0, "fallback": 0, "stale": 0, "missing": 0}
        successes: list[datetime] = []
        local_date = local_now.date()
        for series in catalog.series:
            state = states.get(series.id)
            if (
                state is None or state.catalog_version != catalog.catalog_version
                or not state.active_source or state.latest_observed_at is None
                or state.latest_observed_at > local_date
                or (state.latest_released_at is not None and _as_utc(state.latest_released_at) > _as_utc(now))
            ):
                latest = visible_latest.get(series.id)
                if latest is None:
                    counts["missing"] += 1
                else:
                    # Core history proves availability, not successful collection.
                    # Do not invent success times or resolve failures in this reader.
                    counts["stale"] += 1
                    primary = min(series.sources, key=lambda source: source.priority) if series.sources else None
                    counts["fallback"] += int(is_verified_import(latest) or (
                        primary is not None and latest.source != primary.provider
                    ))
                continue
            counts["fallback"] += int(state.fallback_used)
            if state.last_success_at is not None and _as_utc(state.last_success_at) <= _as_utc(now):
                successes.append(_as_utc(state.last_success_at))
            current = latest_validity.get((series.id, state.active_source, state.latest_observed_at))
            invalidated = current is not None and current.valid_until is not None and _as_utc(current.valid_until) <= _as_utc(now)
            if state.stale or invalidated or observation_expired(series, state.latest_observed_at, local_date):
                counts["stale"] += 1
            else:
                counts["fresh"] += 1
                counts["completed"] += 1
        versions = {row.version for row in snapshots}
        version = next(iter(versions)) if len(snapshots) == 2 and len(versions) == 1 else None
        return {
            "enabled": True,
            "nextRunAt": _timestamp_payload(next_macro_schedule(
                now, self._settings.macro_sync_day_of_week,
                self._settings.macro_sync_time, self._settings.sync_timezone,
            )),
            "lastRun": None if run is None else {
                "jobRunId": run.id, "businessKey": run.business_key, "status": run.status,
                "startedAt": _timestamp_payload(run.started_at),
                "finishedAt": _optional_timestamp_payload(run.finished_at),
            },
            "series": counts,
            "oldestSuccessAt": _optional_timestamp_payload(min(successes) if successes else None),
            "snapshotVersion": version,
            "openFailures": len(failures),
            "failureSummaries": [{
                "jobRunId": failure.job_run_id, "seriesId": failure.dataset.removeprefix("macro:"),
                "provider": failure.request_key, "code": macro_safe_error_code(failure.error_code),
                "message": macro_safe_error_code(failure.error_code), "retryable": failure.retryable,
                "createdAt": _timestamp_payload(failure.created_at),
            } for failure in failures],
            "degraded": bool(
                counts["stale"] or counts["missing"] or version is None
                or any(row.stale for row in snapshots)
                or (substantive is not None and substantive.status in {"partial", "failed"})
            ),
        }


@dataclass(frozen=True, slots=True)
class SyncStatusReport:
    status: SyncStatusKind
    checked_at: datetime
    timezone: str
    scheduler: dict[str, object]
    sync: dict[str, object]
    freshness: dict[str, object]
    alerts: tuple[dict[str, object], ...]
    macro: dict[str, object] = field(default_factory=_disabled_macro_payload)

    def to_payload(self) -> dict[str, object]:
        return deepcopy(
            {
                "status": self.status,
                "checkedAt": _timestamp_payload(self.checked_at),
                "timezone": self.timezone,
                "scheduler": self.scheduler,
                "sync": self.sync,
                "freshness": self.freshness,
                "alerts": list(self.alerts),
                "macro": self.macro,
            }
        )


class SyncStatusService:
    def __init__(
        self,
        *,
        state: SchedulerStateRepository,
        resolver: ExpectedBusinessDateResolver,
        planner: CatchupPlanner,
        freshness: FreshnessService,
        universe: UniverseSelection,
        timezone: str,
        heartbeat_timeout: timedelta,
        catchup_max_days: int = 10,
        scheduler_name: str = "daily-sync",
        macro_status: MacroStatusReader | None = None,
    ) -> None:
        self._state = state
        self._resolver = resolver
        self._planner = planner
        self._freshness = freshness
        self._universe = universe
        self._timezone = timezone
        self._heartbeat_timeout = heartbeat_timeout
        self._catchup_max_days = catchup_max_days
        self._scheduler_name = scheduler_name
        self._macro_status = macro_status

    def check(self, now: datetime) -> SyncStatusReport:
        _require_timezone_aware(now)
        checked_at = _as_utc(now)
        macro = self._macro_status.read(checked_at) if self._macro_status else _disabled_macro_payload()
        heartbeat = self._state.latest_instance(self._scheduler_name)
        scheduler, scheduler_healthy = self._scheduler_payload(heartbeat, checked_at)
        checkpoint = self._state.checkpoint(self._universe)
        latest_run = self._state.latest_daily_run(self._universe)
        consecutive_failures = self._state.consecutive_failures(self._universe)
        alerts = tuple(
            self._alert_payload(alert)
            for alert in sorted(self._state.open_alerts(), key=lambda item: item.key)
        )
        try:
            expected = self._resolver.resolve(now)
            plan = self._planner.inspect(expected)
        except TradeCalendarUnavailable as error:
            return SyncStatusReport(
                status="degraded",
                checked_at=checked_at,
                timezone=self._timezone,
                scheduler=scheduler,
                sync={
                    "expectedBusinessDate": None,
                    "expectedBusinessDateError": {
                        "code": error.error_code,
                        "message": "trade calendar unavailable",
                    },
                    "managedFrom": (
                        checkpoint.managed_from.isoformat()
                        if checkpoint is not None
                        else None
                    ),
                    "universe": {
                        "profile": self._universe.profile,
                        "version": self._universe.version,
                    },
                    "lastRun": self._daily_run_payload(latest_run),
                    "consecutiveFailures": consecutive_failures,
                    "catchup": {
                        "assessed": False,
                        "maxDays": self._catchup_max_days,
                        "backlogCount": None,
                        "missingBusinessDates": [],
                        "catchupLimitExceeded": None,
                    },
                },
                freshness={
                    "assessed": False,
                    "checkedAt": _timestamp_payload(checked_at),
                    "staleProducts": [],
                    "missingProducts": [],
                },
                alerts=alerts,
                macro=macro,
            )
        freshness_report = self._freshness.inspect(
            expected_business_date=expected,
            missing_business_dates=plan.missing_dates,
            checked_at=checked_at,
        )
        sync = {
            "expectedBusinessDate": expected.isoformat(),
            "managedFrom": (
                checkpoint.managed_from.isoformat() if checkpoint is not None else None
            ),
            "universe": {
                "profile": self._universe.profile,
                "version": self._universe.version,
            },
            "lastRun": self._daily_run_payload(latest_run),
            "consecutiveFailures": consecutive_failures,
            "catchup": {
                "assessed": True,
                "maxDays": plan.max_days,
                "backlogCount": plan.backlog_count,
                "missingBusinessDates": [
                    business_date.isoformat()
                    for business_date in sorted(plan.missing_dates)
                ],
                "catchupLimitExceeded": plan.limit_exceeded,
            },
        }
        freshness = {
            "assessed": True,
            "checkedAt": _timestamp_payload(freshness_report.checked_at),
            "staleProducts": sorted(freshness_report.stale_products),
            "missingProducts": sorted(freshness_report.missing_products),
        }
        healthy = (
            scheduler_healthy
            and checkpoint is not None
            and consecutive_failures == 0
            and not plan.missing_dates
            and freshness_report.healthy
            and not any(
                macro["enabled"] or alert["key"] != "macro-sync:degraded"
                for alert in alerts
            )
            and not (macro["enabled"] and macro["degraded"])
        )
        return SyncStatusReport(
            status="ok" if healthy else "degraded",
            checked_at=checked_at,
            timezone=self._timezone,
            scheduler=scheduler,
            sync=sync,
            freshness=freshness,
            alerts=alerts,
            macro=macro,
        )

    def _scheduler_payload(
        self,
        heartbeat: HeartbeatView | None,
        checked_at: datetime,
    ) -> tuple[dict[str, object], bool]:
        if heartbeat is None:
            return (
                {
                    "online": False,
                    "instanceId": None,
                    "state": None,
                    "startedAt": None,
                    "lastHeartbeatAt": None,
                    "nextRunAt": None,
                },
                False,
            )
        last_heartbeat_at = _as_utc(heartbeat.last_heartbeat_at)
        online = (
            heartbeat.state != "stopping"
            and checked_at - last_heartbeat_at <= self._heartbeat_timeout
        )
        return (
            {
                "online": online,
                "instanceId": heartbeat.instance_id,
                "state": heartbeat.state,
                "startedAt": _timestamp_payload(heartbeat.started_at),
                "lastHeartbeatAt": _timestamp_payload(heartbeat.last_heartbeat_at),
                "nextRunAt": _optional_timestamp_payload(heartbeat.next_run_at),
            },
            online and heartbeat.state != "degraded",
        )

    @staticmethod
    def _daily_run_payload(run: DailyRunView | None) -> dict[str, object] | None:
        if run is None:
            return None
        return {
            "status": run.status,
            "businessDate": run.business_date.isoformat(),
            "startedAt": _timestamp_payload(run.started_at),
            "finishedAt": _optional_timestamp_payload(run.finished_at),
        }

    @staticmethod
    def _alert_payload(alert: AlertView) -> dict[str, object]:
        return {
            "key": alert.key,
            "alertType": alert.alert_type,
            "severity": alert.severity,
            "status": alert.status,
            "firstTriggeredAt": _timestamp_payload(alert.first_triggered_at),
            "lastTriggeredAt": _timestamp_payload(alert.last_triggered_at),
        }


def _require_timezone_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_payload(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _optional_timestamp_payload(value: datetime | None) -> str | None:
    return _timestamp_payload(value) if value is not None else None
