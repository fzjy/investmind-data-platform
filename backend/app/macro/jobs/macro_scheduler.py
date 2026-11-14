from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, time, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.jobs.locks import JobLock
from app.macro.jobs.macro_sync import MacroSyncReport
from app.macro.jobs.macro_sync_history import MacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
from app.jobs.scheduler_state import SchedulerStateRepository
from app.macro.catalog import MacroSeriesCatalog


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _slot(day: date, local_time: time, zone: ZoneInfo) -> datetime:
    # First occurrence on a fold; round-trip advances a nonexistent wall time
    # across the spring gap. Compare instants in UTC, never wall-time datetimes.
    return datetime.combine(day, local_time, zone).astimezone(UTC)


def most_recent_macro_schedule(
    now: datetime, weekday: str, local_time: time, timezone: str,
) -> datetime:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    day = local.date() - timedelta(days=(local.weekday() - _WEEKDAYS.index(weekday)) % 7)
    slot = _slot(day, local_time, zone)
    if slot > now.astimezone(UTC):
        slot = _slot(day - timedelta(days=7), local_time, zone)
    return slot


def next_macro_schedule(
    now: datetime, weekday: str, local_time: time, timezone: str,
) -> datetime:
    latest = most_recent_macro_schedule(now, weekday, local_time, timezone)
    zone = ZoneInfo(timezone)
    day = latest.astimezone(zone).date() + timedelta(days=7)
    return _slot(day, local_time, zone)


class MacroWeeklyCronTrigger(CronTrigger):
    """One weekly instant: first fold, forward through a spring gap."""

    def __init__(self, weekday: str, local_time: time, timezone: str) -> None:
        super().__init__(
            day_of_week=weekday, hour=local_time.hour, minute=local_time.minute,
            timezone=timezone,
        )

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime,
    ) -> datetime:
        # Use CronTrigger's serialized fields, but the same instant policy as
        # startup/status. Native cron emits both folds and skips a spring-gap
        # slot when starting after the clock jump but before that slot's instant.
        fields = {field.name: str(field) for field in self.fields}
        local_time = time(int(fields["hour"]), int(fields["minute"]))
        earliest = (
            previous_fire_time.astimezone(UTC) + timedelta(microseconds=1)
            if previous_fire_time is not None else now.astimezone(UTC)
        )
        latest = most_recent_macro_schedule(
            earliest, fields["day_of_week"], local_time, str(self.timezone),
        )
        if latest == earliest:
            return latest
        return next_macro_schedule(earliest, fields["day_of_week"], local_time, str(self.timezone))


class MacroScheduledCoordinator:
    def __init__(
        self, *, settings: Settings, catalog: MacroSeriesCatalog,
        history: MacroSyncHistory, state: SchedulerStateRepository,
        runtime_factory: Callable[[], AbstractContextManager[MacroSyncRuntime]],
        publication_lock: JobLock,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._history = history
        self._state = state
        self._runtime_factory = runtime_factory
        # Kept as a constructor compatibility argument; the real job owns the
        # shared lock for CLI and scheduler alike. Never acquire it twice.
        del publication_lock
        self._clock = clock
        self._startup_lock = Lock()
        self._startup_attempted = False
        self._last_slot: datetime | None = None

    def run_startup(self, now: datetime) -> MacroSyncReport | None:
        with self._startup_lock:
            if self._startup_attempted:
                return None
            self._startup_attempted = True
        if not self._settings.macro_sync_catchup_on_start:
            return None
        return self.run_scheduled(now)

    def run_scheduled(self, now: datetime) -> MacroSyncReport | None:
        if not self._settings.macro_sync_enabled:
            return None
        settings = self._settings
        slot = most_recent_macro_schedule(
            now, settings.macro_sync_day_of_week, settings.macro_sync_time,
            settings.sync_timezone,
        )
        with self._startup_lock:
            if self._last_slot is not None and slot <= self._last_slot:
                return None
            self._last_slot = slot
        catalog = self._catalog
        key = macro_sync_business_key(slot, catalog.catalog_version, settings.sync_timezone)
        completed, attempt = self._history.inspect(key, catalog.catalog_version, catalog.catalog_hash)
        if completed:
            return None
        options = MacroSyncOptions(
            slot, settings.macro_history_years, catalog.catalog_version,
            catalog.catalog_hash, trigger="scheduler", mode="live",
        )
        with self._runtime_factory() as runtime:
            return runtime.job.run(options, on_completed=self._record_health)

    def _record_health(self, report: MacroSyncReport) -> MacroSyncReport:
        if report.status in {"partial", "failed"}:
            self._state.open_alert(
                "macro-sync:degraded", "macro_sync",
                "warning" if report.status == "partial" else "error", self._clock(),
                {"jobRunId": report.job_run_id, "status": report.status},
            )
        elif report.status == "completed":
            self._state.resolve_alert("macro-sync:degraded", self._clock())
        return report
