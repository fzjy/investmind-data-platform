from __future__ import annotations

import json
import logging
import sys
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import Any

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.base import run_job
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from pydantic import SecretStr

from app.config import Settings
from app.connectors.tushare.strategies import TushareMode
from app.db.base import Base
from app.db.models import JobRun, PublishedSnapshot, TradeCalendar
from app.db.session import create_engine_and_session
from app.jobs import scheduler as scheduler_module
from app.stocks.jobs.daily_sync_runtime import DailySyncRuntime
from app.stocks.jobs.daily_sync_history import daily_sync_business_key
from app.jobs.freshness import FreshnessService
from app.jobs.scheduler import (
    SchedulerRuntime,
    build_scheduler_runtime,
    configure_scheduler,
    create_scheduler,
)
from app.stocks.jobs.scheduler_coordinator import ScheduledDailySyncCoordinator
from app.jobs.scheduler_logging import SchedulerJsonLogger
from app.stocks.jobs.scheduler_planning import CatchupPlanner, ExpectedBusinessDateResolver
from app.jobs.scheduler_state import SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection


class RecordingCoordinator:
    def run_scheduled(self, now: datetime) -> None:
        del now

    def run_startup(self, now: datetime) -> None:
        del now


def test_scheduler_factory_does_not_start_jobs() -> None:
    scheduler = create_scheduler(Settings())

    assert isinstance(scheduler, BlockingScheduler)
    assert scheduler.running is False


def test_scheduler_registers_all_cross_platform_jobs() -> None:
    settings = Settings(
        sync_hour=19,
        sync_minute=7,
        scheduler_heartbeat_seconds=60,
        scheduler_heartbeat_timeout_seconds=180,
        sync_freshness_interval_seconds=300,
    )
    scheduler = configure_scheduler(
        create_scheduler(settings),
        settings,
        RecordingCoordinator(),
        clock=lambda: datetime(2026, 9, 8, 11, 0, tzinfo=UTC),
        heartbeat_job=lambda: None,
        freshness_job=lambda: None,
    )

    daily = scheduler.get_job("sync-daily")
    assert daily is not None
    assert str(daily.trigger) == "cron[hour='19', minute='7']"
    assert daily.max_instances == 1
    assert daily.coalesce is True
    assert scheduler.get_job("scheduler-heartbeat") is not None
    assert scheduler.get_job("snapshot-freshness") is not None
    assert scheduler.get_job("startup-catchup") is not None
    assert scheduler.get_job("sync-announcements") is None


def test_announcement_job_shares_configured_daily_schedule_without_starting_it():
    settings = Settings(sync_hour=19, sync_minute=7)
    scheduler = configure_scheduler(create_scheduler(settings), settings, RecordingCoordinator(),
                                    announcement_job=lambda: None)
    job = scheduler.get_job("sync-announcements")
    assert str(job.trigger) == str(scheduler.get_job("sync-daily").trigger)
    assert job.max_instances == 1 and job.coalesce


def test_three_argument_configure_scheduler_registers_all_jobs() -> None:
    settings = Settings()

    scheduler = configure_scheduler(
        create_scheduler(settings),
        settings,
        RecordingCoordinator(),
    )

    assert {job.id for job in scheduler.get_jobs()} == {
        "sync-daily",
        "scheduler-heartbeat",
        "snapshot-freshness",
        "startup-catchup",
    }


def test_startup_catchup_has_unlimited_misfire_grace() -> None:
    settings = Settings()
    scheduler = configure_scheduler(
        create_scheduler(settings),
        settings,
        RecordingCoordinator(),
    )

    startup = scheduler.get_job("startup-catchup")
    assert startup is not None
    assert startup.misfire_grace_time is None


def test_freshness_runs_at_startup_then_keeps_configured_interval() -> None:
    settings = Settings(sync_freshness_interval_seconds=300)
    scheduler = configure_scheduler(
        create_scheduler(settings),
        settings,
        RecordingCoordinator(),
        clock=lambda: NOW,
    )

    freshness = scheduler.get_job("snapshot-freshness")
    assert freshness is not None
    assert freshness.next_run_time == NOW
    assert freshness.trigger.interval == timedelta(seconds=300)


def test_scheduler_source_never_uses_signal_pause() -> None:
    source = Path(scheduler_module.__file__).read_text(encoding="utf-8")

    assert "signal.pause" not in source


@pytest.mark.parametrize("enabled", [True, False])
def test_macro_registration_uses_config_and_single_startup_job(enabled):
    settings = Settings(macro_sync_enabled=enabled, macro_sync_day_of_week="wed", macro_sync_time="10:15")
    calls = []
    scheduler = configure_scheduler(
        create_scheduler(settings), settings, RecordingCoordinator(),
        macro_job=lambda: calls.append("macro"),
    )
    macro = scheduler.get_job("sync-macro")
    if enabled:
        assert str(macro.trigger) == "cron[day_of_week='wed', hour='10', minute='15']"
        assert str(macro.trigger.timezone) == "Asia/Shanghai"
        assert macro.max_instances == 1 and macro.coalesce is True
        macro.func()
        assert calls == ["macro"]
    else:
        assert macro is None
    assert [job.id for job in scheduler.get_jobs() if str(job.trigger).startswith("date")] == ["startup-catchup"]


@pytest.mark.parametrize(("local_time", "now", "expected"), [
    ("01:30", "2026-11-01T05:00:00+00:00", "2026-11-01T05:30:00+00:00"),
    ("01:30", "2026-11-01T05:45:00+00:00", "2026-11-08T06:30:00+00:00"),
    ("01:30", "2026-11-01T06:15:00+00:00", "2026-11-08T06:30:00+00:00"),
    ("01:30", "2026-11-01T06:45:00+00:00", "2026-11-08T06:30:00+00:00"),
    ("02:30", "2026-03-08T06:45:00+00:00", "2026-03-08T07:30:00+00:00"),
    ("02:30", "2026-03-08T07:15:00+00:00", "2026-03-08T07:30:00+00:00"),
    ("02:30", "2026-03-08T07:45:00+00:00", "2026-03-15T06:30:00+00:00"),
])
def test_registered_macro_cron_matches_schedule_helpers_across_dst(local_time, now, expected):
    import pickle
    from datetime import time
    from apscheduler.triggers.cron import CronTrigger
    from app.macro.jobs.macro_scheduler import most_recent_macro_schedule, next_macro_schedule
    settings = Settings(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time=local_time)
    scheduler = configure_scheduler(create_scheduler(settings), settings, RecordingCoordinator(), macro_job=lambda: None)
    trigger = scheduler.get_job("sync-macro").trigger
    assert isinstance(trigger, CronTrigger)
    current = datetime.fromisoformat(now)
    instant = datetime.fromisoformat(expected)
    assert trigger.get_next_fire_time(None, current).astimezone(UTC) == instant
    assert next_macro_schedule(current, "sun", time.fromisoformat(local_time), "America/New_York") == instant
    assert most_recent_macro_schedule(instant, "sun", time.fromisoformat(local_time), "America/New_York") == instant
    # Inclusive first-fire boundary versus strictly future status nextRunAt.
    first = trigger.get_next_fire_time(None, instant)
    assert first.astimezone(UTC) == instant
    after = trigger.get_next_fire_time(first, instant + timedelta(microseconds=1)).astimezone(UTC)
    assert after == next_macro_schedule(instant, "sun", time.fromisoformat(local_time), "America/New_York")
    restored = pickle.loads(pickle.dumps(trigger))
    assert type(restored) is type(trigger)
    assert str(restored) == str(trigger) and str(restored.timezone) == "America/New_York"
    assert restored.get_next_fire_time(None, current) == instant
    assert restored.get_next_fire_time(first, instant + timedelta(microseconds=1)) == after


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_real_registered_cron_runs_once_per_week_even_when_attempt_fails(status):
    from contextlib import contextmanager
    from app.macro.jobs.macro_sync import MacroSyncReport
    from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
    from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.jobs.locks import ProcessLocalJobLock
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
    settings = Settings(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time="01:30")
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    calls = []
    class Job:
        def run(self, options, *, on_completed):
            calls.append(options.as_of)
            return on_completed(MacroSyncReport(status, "run-1", "key", 1, options.as_of, catalog.catalog_version))
    @contextmanager
    def runtime():
        yield MacroSyncRuntime(Job(), catalog)
    coordinator = MacroScheduledCoordinator(
        settings=settings, catalog=catalog, history=SqlMacroSyncHistory(factory),
        state=SchedulerStateRepository(factory), runtime_factory=runtime,
        publication_lock=ProcessLocalJobLock(),
    )
    scheduler = configure_scheduler(create_scheduler(settings), settings, RecordingCoordinator(), macro_job=lambda: None)
    trigger = scheduler.get_job("sync-macro").trigger
    now = datetime.fromisoformat("2026-11-01T05:00:00+00:00")
    limit = datetime.fromisoformat("2026-11-09T00:00:00+00:00")
    fires = []
    previous = None
    try:
        for _ in range(4):
            scheduled = trigger.get_next_fire_time(previous, now)
            fire = scheduled.astimezone(UTC)
            if fire >= limit:
                break
            fires.append(fire)
            coordinator.run_scheduled(fire)
            previous, now = scheduled, fire + timedelta(microseconds=1)
        assert fires == [datetime.fromisoformat("2026-11-01T05:30:00+00:00"), datetime.fromisoformat("2026-11-08T06:30:00+00:00")]
        assert calls == fires
    finally:
        engine.dispose()


def test_installed_scheduler_coalesces_missed_macro_weeks_across_fold(monkeypatch):
    import apscheduler.schedulers.base as base_module
    from apscheduler.executors.debug import DebugExecutor
    now = datetime.fromisoformat("2026-11-09T00:00:00+00:00")
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(base_module, "datetime", FixedClock)
    settings = Settings(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time="01:30")
    calls, outcomes = [], []
    scheduler = configure_scheduler(
        create_scheduler(settings), settings, RecordingCoordinator(), clock=lambda: now,
        macro_job=lambda: calls.append("macro"),
    )
    for job in scheduler.get_jobs():
        if job.id != "sync-macro":
            scheduler.remove_job(job.id)
    scheduler.add_executor(DebugExecutor(), "default")
    scheduler._event = Event()
    BaseScheduler.start(scheduler, paused=True)
    try:
        job = scheduler.get_job("sync-macro")
        job.modify(next_run_time=datetime.fromisoformat("2026-10-25T05:30:00+00:00"), misfire_grace_time=None)
        missed = [
            datetime.fromisoformat("2026-10-25T05:30:00+00:00"),
            datetime.fromisoformat("2026-11-01T05:30:00+00:00"),
            datetime.fromisoformat("2026-11-08T06:30:00+00:00"),
        ]
        assert job._get_run_times(now) == missed
        assert job.coalesce is True
        scheduler.add_listener(outcomes.append, EVENT_JOB_EXECUTED)
        BaseScheduler.resume(scheduler)
        scheduler._process_jobs()
        assert calls == ["macro"]
        assert len(outcomes) == 1 and outcomes[0].scheduled_run_time == missed[-1]
        assert job.next_run_time == datetime.fromisoformat("2026-11-15T06:30:00+00:00")
        scheduler._process_jobs()
        assert calls == ["macro"]
    finally:
        scheduler.shutdown(wait=True)


def test_runtime_runs_macro_startup_despite_daily_failure_and_records_health(monkeypatch):
    events = []
    runtime = _runtime(events)
    calls = []

    class Macro:
        def run_startup(self, now):
            calls.append(("startup", now))
            return SimpleNamespace(status="partial")

        def run_scheduled(self, now):
            calls.append(("scheduled", now))
            return SimpleNamespace(status="completed")

    runtime._macro_coordinator = Macro()

    def fail_daily(now):
        raise RuntimeError("daily unavailable")

    coordinator = RuntimeCoordinator(events)
    coordinator.run_startup = fail_daily
    runtime._coordinator_factory = lambda _: coordinator

    def start(scheduler):
        with pytest.raises(scheduler_module.SchedulerJobError):
            scheduler.get_job("startup-catchup").func()
        assert calls == [("startup", NOW)]
        assert "macro" in runtime._degraded_reasons
        scheduler.get_job("sync-macro").func()
        assert calls[-1] == ("scheduled", NOW)
        assert "macro" not in runtime._degraded_reasons
        assert "catchup" in runtime._degraded_reasons

    monkeypatch.setattr(BlockingScheduler, "start", start)
    runtime.run()


NOW = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
NEXT_RUN = datetime(2026, 9, 8, 11, 30, tzinfo=UTC)
UNIVERSE = UniverseSelection(
    profile="custom",
    version="custom-v1",
    coverage="custom",
    symbols=("000001.SZ",),
    fingerprint="fingerprint",
)


class RecordingState:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.current_state = "running"
        self.fail_heartbeat_once = False
        self.fail_stop = False
        self.trading_day_value: bool | None = True
        self.calendar_alert_open = False
        self.fail_operational_touch_once = False
        self.fail_alert_once = False

    def start_instance(
        self,
        scheduler_name: str,
        *,
        now: datetime,
        details: dict[str, object],
    ) -> str:
        self.events.append(("state.start", scheduler_name, now, details))
        return "scheduler-1"

    def touch_instance(
        self,
        instance_id: str,
        *,
        now: datetime,
        next_run_at: datetime | None,
        state: str,
    ) -> SimpleNamespace:
        if self.fail_operational_touch_once and state == "degraded":
            self.fail_operational_touch_once = False
            raise RuntimeError("temporary state write failure")
        if self.fail_heartbeat_once and next_run_at is not None:
            self.fail_heartbeat_once = False
            raise RuntimeError("temporary database secret")
        self.current_state = state
        self.events.append(
            ("state.touch", instance_id, now, next_run_at, state)
        )
        return SimpleNamespace(state=state)

    def heartbeat(self, instance_id: str) -> SimpleNamespace:
        self.events.append(("state.heartbeat", instance_id))
        return SimpleNamespace(state=self.current_state)

    def stop_instance(self, instance_id: str, *, now: datetime) -> None:
        self.events.append(("state.stop", instance_id, now))
        if self.fail_stop:
            raise RuntimeError("cleanup failed")

    def trading_day(self, business_date) -> bool | None:
        self.events.append(("state.trading_day", business_date))
        return self.trading_day_value

    def alert(self, key: str) -> SimpleNamespace | None:
        self.events.append(("state.alert", key))
        if self.fail_alert_once:
            self.fail_alert_once = False
            raise RuntimeError("temporary alert read failure")
        if not self.calendar_alert_open:
            return None
        return SimpleNamespace(
            status="open",
            alert_type="trade-calendar-unavailable",
        )


class InterleavingState(RecordingState):
    def __init__(self, events: list[object]) -> None:
        super().__init__(events)
        self.heartbeat_paused = Event()
        self.release_heartbeat = Event()
        self.transition_write_completed = Event()

    def touch_instance(
        self,
        instance_id: str,
        *,
        now: datetime,
        next_run_at: datetime | None,
        state: str,
    ) -> SimpleNamespace:
        thread_name = current_thread().name
        if thread_name == "heartbeat-transition":
            self.heartbeat_paused.set()
            assert self.release_heartbeat.wait(timeout=1)
        result = super().touch_instance(
            instance_id,
            now=now,
            next_run_at=next_run_at,
            state=state,
        )
        if thread_name == "freshness-transition":
            self.transition_write_completed.set()
        return result


class RuntimeCoordinator(RecordingCoordinator):
    def __init__(
        self,
        events: list[object],
        *,
        fail_recovery: bool = False,
        daily_report: object | None = None,
        startup_report: object | None = None,
    ) -> None:
        self.events = events
        self.fail_recovery = fail_recovery
        self.daily_report = daily_report or SimpleNamespace(
            status="completed",
            business_date=NOW.date(),
        )
        self.startup_report = startup_report or SimpleNamespace(
            attempted_dates=(NOW.date(),),
            remaining_dates=(),
            reports=(),
        )

    def recover_interrupted(self, now: datetime) -> tuple[str, ...]:
        self.events.append(("coordinator.recover", now))
        if self.fail_recovery:
            raise RuntimeError("recovery failed")
        return ("run-1",)

    def run_scheduled(self, now: datetime) -> SimpleNamespace:
        self.events.append(("coordinator.daily", now))
        return self.daily_report

    def run_startup(self, now: datetime) -> SimpleNamespace:
        self.events.append(("coordinator.startup", now))
        return self.startup_report


class RecordingLogger:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def emit(self, event: str, level: str, **fields: object) -> None:
        self.events.append(("log", event, level, fields))


def _runtime(
    events: list[object],
    *,
    state: RecordingState | None = None,
    coordinator: RuntimeCoordinator | None = None,
    freshness_runner: Any = None,
    prepare_checkpoint: Any = None,
    logger_factory: Any = None,
) -> SchedulerRuntime:
    active_state = state or RecordingState(events)
    active_coordinator = coordinator or RuntimeCoordinator(events)

    def coordinator_factory(instance_id: str) -> RuntimeCoordinator:
        events.append(("coordinator.factory", instance_id))
        return active_coordinator

    return SchedulerRuntime(
        settings=Settings(),
        state=active_state,
        universe=UNIVERSE,
        coordinator_factory=coordinator_factory,
        prepare_checkpoint=prepare_checkpoint
        or (lambda now: events.append(("checkpoint.prepare", now))),
        freshness_runner=freshness_runner
        or (lambda now: events.append(("freshness.refresh", now))),
        clock=lambda: NOW,
        logger_factory=logger_factory
        or (lambda instance_id: RecordingLogger(events)),
    )


def test_runtime_orders_startup_and_defers_catchup_until_scheduler_start(
    monkeypatch,
) -> None:
    events: list[object] = []

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        events.append("scheduler.start")
        assert not any(event[0] == "coordinator.startup" for event in events if isinstance(event, tuple))
        startup = scheduler.get_job("startup-catchup")
        assert startup is not None
        startup.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events).run()

    lifecycle_events = [
        event
        for event in events
        if not (isinstance(event, tuple) and event[0] == "log")
    ]
    assert lifecycle_events[:6] == [
        ("state.start", "daily-sync", NOW, UNIVERSE.to_payload()),
        ("state.touch", "scheduler-1", NOW, None, "starting"),
        ("coordinator.factory", "scheduler-1"),
        ("coordinator.recover", NOW),
        ("checkpoint.prepare", NOW),
        ("state.touch", "scheduler-1", NOW, None, "running"),
    ]
    assert events.index("scheduler.start") < events.index(("coordinator.startup", NOW))
    assert events[-1] == ("state.stop", "scheduler-1", NOW)


def test_runtime_heartbeat_preserves_state_and_records_daily_next_run(
    monkeypatch,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.current_state = "degraded"

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        state.current_state = "degraded"
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        daily.next_run_time = NEXT_RUN
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        assert heartbeat is not None
        heartbeat.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events, state=state).run()

    assert (
        "state.touch",
        "scheduler-1",
        NOW,
        NEXT_RUN,
        "degraded",
    ) in events


def test_interval_failures_are_logged_and_do_not_escape(
    monkeypatch,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.fail_heartbeat_once = True
    freshness_calls = 0

    def freshness_runner(now: datetime) -> None:
        nonlocal freshness_calls
        freshness_calls += 1
        if freshness_calls == 1:
            raise RuntimeError("freshness secret")
        events.append(("freshness.refresh", now))

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        daily.next_run_time = NEXT_RUN
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        freshness = scheduler.get_job("snapshot-freshness")
        assert heartbeat is not None
        assert freshness is not None
        heartbeat.func()
        heartbeat.func()
        freshness.func()
        freshness.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events, state=state, freshness_runner=freshness_runner).run()

    assert any(
        event[:3] == ("log", "scheduler.heartbeat", "error")
        for event in events
        if isinstance(event, tuple)
    )
    assert any(
        event[:3] == ("log", "scheduler.freshness.failed", "error")
        for event in events
        if isinstance(event, tuple)
    )
    assert freshness_calls == 2


def test_heartbeat_retries_failed_operational_state_write(monkeypatch) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.fail_operational_touch_once = True
    coordinator = RuntimeCoordinator(
        events,
        daily_report=SimpleNamespace(
            status="failed",
            business_date=NOW.date(),
        ),
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        assert daily is not None
        assert heartbeat is not None
        daily.next_run_time = NEXT_RUN
        daily.func()
        assert state.current_state == "running"
        heartbeat.func()
        assert state.current_state == "degraded"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, state=state, coordinator=coordinator).run()

    assert (
        "state.touch",
        "scheduler-1",
        NOW,
        NEXT_RUN,
        "degraded",
    ) in events


def test_heartbeat_does_not_overwrite_recovered_running_state(monkeypatch) -> None:
    events: list[object] = []
    state = InterleavingState(events)
    reports = deque(
        (
            SimpleNamespace(healthy=False, stale_products=("screener",)),
            SimpleNamespace(healthy=True, stale_products=()),
        )
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        freshness = scheduler.get_job("snapshot-freshness")
        assert heartbeat is not None
        assert freshness is not None
        freshness.func()
        assert state.current_state == "degraded"

        heartbeat_thread = Thread(
            target=heartbeat.func,
            name="heartbeat-transition",
        )
        freshness_thread = Thread(
            target=freshness.func,
            name="freshness-transition",
        )
        heartbeat_thread.start()
        assert state.heartbeat_paused.wait(timeout=1)
        freshness_thread.start()
        state.transition_write_completed.wait(timeout=1)
        state.release_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        freshness_thread.join(timeout=1)

        assert not heartbeat_thread.is_alive()
        assert not freshness_thread.is_alive()
        assert state.current_state == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(
        events,
        state=state,
        freshness_runner=lambda _now: reports.popleft(),
    ).run()


def test_heartbeat_does_not_overwrite_new_degraded_state(monkeypatch) -> None:
    events: list[object] = []
    state = InterleavingState(events)

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        freshness = scheduler.get_job("snapshot-freshness")
        assert heartbeat is not None
        assert freshness is not None

        heartbeat_thread = Thread(
            target=heartbeat.func,
            name="heartbeat-transition",
        )
        freshness_thread = Thread(
            target=freshness.func,
            name="freshness-transition",
        )
        heartbeat_thread.start()
        assert state.heartbeat_paused.wait(timeout=1)
        freshness_thread.start()
        state.transition_write_completed.wait(timeout=1)
        state.release_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        freshness_thread.join(timeout=1)

        assert not heartbeat_thread.is_alive()
        assert not freshness_thread.is_alive()
        assert state.current_state == "degraded"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(
        events,
        state=state,
        freshness_runner=lambda _now: SimpleNamespace(
            healthy=False,
            stale_products=("screener",),
        ),
    ).run()


def test_keyboard_interrupt_stops_cleanly(monkeypatch) -> None:
    events: list[object] = []

    def interrupt(_scheduler: BlockingScheduler) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(BlockingScheduler, "start", interrupt)

    _runtime(events).run()

    assert events[-1] == ("state.stop", "scheduler-1", NOW)


def test_cleanup_failure_does_not_replace_primary_error(monkeypatch) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.fail_stop = True
    coordinator = RuntimeCoordinator(events, fail_recovery=True)
    monkeypatch.setattr(
        BlockingScheduler,
        "start",
        lambda _scheduler: pytest.fail("scheduler must not start after recovery failure"),
    )

    with pytest.raises(RuntimeError, match="recovery failed"):
        _runtime(events, state=state, coordinator=coordinator).run()


def test_calendar_unavailable_during_prepare_starts_degraded(monkeypatch) -> None:
    events: list[object] = []

    def start_without_blocking(_scheduler: BlockingScheduler) -> None:
        touches = [
            event
            for event in events
            if isinstance(event, tuple) and event[0] == "state.touch"
        ]
        assert touches[-1] == (
            "state.touch",
            "scheduler-1",
            NOW,
            None,
            "degraded",
        )

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events, prepare_checkpoint=lambda _now: False).run()


@pytest.mark.parametrize("stop_signal", (KeyboardInterrupt, SystemExit))
def test_stop_signal_shuts_down_running_scheduler_without_escaping(
    monkeypatch,
    stop_signal: type[BaseException],
) -> None:
    events: list[object] = []

    def interrupt(scheduler: BlockingScheduler) -> None:
        scheduler.state = 1
        raise stop_signal

    def shutdown(_scheduler: BlockingScheduler, *, wait: bool) -> None:
        events.append(("scheduler.shutdown", wait))

    monkeypatch.setattr(BlockingScheduler, "start", interrupt)
    monkeypatch.setattr(BlockingScheduler, "shutdown", shutdown)

    _runtime(events).run()

    assert ("scheduler.shutdown", True) in events


def test_shutdown_failure_does_not_replace_runtime_error(monkeypatch) -> None:
    events: list[object] = []

    def fail_start(scheduler: BlockingScheduler) -> None:
        scheduler.state = 1
        raise RuntimeError("primary scheduler failure")

    def fail_shutdown(_scheduler: BlockingScheduler, *, wait: bool) -> None:
        del wait
        raise RuntimeError("shutdown failure")

    monkeypatch.setattr(BlockingScheduler, "start", fail_start)
    monkeypatch.setattr(BlockingScheduler, "shutdown", fail_shutdown)

    with pytest.raises(RuntimeError, match="primary scheduler failure"):
        _runtime(events).run()


def test_daily_and_startup_jobs_emit_stable_outcome_events(monkeypatch) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(
        events,
        startup_report=SimpleNamespace(
            attempted_dates=(NOW.date(),) * 10,
            remaining_dates=(NOW.date(),),
            reports=(),
        ),
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        startup = scheduler.get_job("startup-catchup")
        assert daily is not None
        assert startup is not None
        daily.func()
        startup.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events, coordinator=coordinator).run()

    logged_events = [
        event[1]
        for event in events
        if isinstance(event, tuple) and event[0] == "log"
    ]
    assert "scheduler.daily.started" in logged_events
    assert "scheduler.daily.completed" in logged_events
    assert "scheduler.catchup.started" in logged_events
    assert "scheduler.catchup.limit-exceeded" in logged_events
    assert "scheduler.catchup.completed" in logged_events


def test_missing_daily_calendar_marks_scheduler_degraded(monkeypatch) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.trading_day_value = None
    state.calendar_alert_open = True
    coordinator = RuntimeCoordinator(events)
    coordinator.daily_report = None

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        daily.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(events, state=state, coordinator=coordinator).run()

    assert any(
        event[:3] == ("log", "scheduler.daily.failed", "error")
        and event[3]["error_code"] == "TRADE_CALENDAR_UNAVAILABLE"
        for event in events
        if isinstance(event, tuple)
    )
    assert any(
        event[0] == "state.touch" and event[-1] == "degraded"
        for event in events
        if isinstance(event, tuple)
    )


def test_unhealthy_freshness_marks_scheduler_degraded(monkeypatch) -> None:
    events: list[object] = []

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        freshness = scheduler.get_job("snapshot-freshness")
        assert freshness is not None
        freshness.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)

    _runtime(
        events,
        freshness_runner=lambda _now: SimpleNamespace(
            healthy=False,
            stale_products=("screener",),
            missing_products=(),
        ),
    ).run()

    assert any(
        event[0] == "state.touch" and event[-1] == "degraded"
        for event in events
        if isinstance(event, tuple)
    )


def test_daily_degraded_reason_recovers_after_completed_run(monkeypatch) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(events)

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        coordinator.daily_report = SimpleNamespace(
            status="failed",
            business_date=NOW.date(),
        )
        daily.func()
        coordinator.daily_report = SimpleNamespace(
            status="completed",
            business_date=NOW.date(),
        )
        daily.func()
        assert _last_runtime_state(events) == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, coordinator=coordinator).run()


def test_catchup_degraded_reason_recovers_when_backlog_clears(monkeypatch) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(events)

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        startup = scheduler.get_job("startup-catchup")
        assert startup is not None
        coordinator.startup_report = SimpleNamespace(
            attempted_dates=(NOW.date(),) * 10,
            remaining_dates=(NOW.date(),),
            reports=(),
        )
        startup.func()
        coordinator.startup_report = SimpleNamespace(
            attempted_dates=(NOW.date(),),
            remaining_dates=(),
            reports=(),
        )
        startup.func()
        assert _last_runtime_state(events) == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, coordinator=coordinator).run()


def test_freshness_degraded_reason_recovers_when_report_is_healthy(
    monkeypatch,
) -> None:
    events: list[object] = []
    reports = deque(
        (
            SimpleNamespace(healthy=False, stale_products=("screener",), missing_products=()),
            SimpleNamespace(healthy=True, stale_products=(), missing_products=()),
        )
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        freshness = scheduler.get_job("snapshot-freshness")
        assert freshness is not None
        freshness.func()
        freshness.func()
        assert _last_runtime_state(events) == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, freshness_runner=lambda _now: reports.popleft()).run()


def test_calendar_degraded_reason_recovers_when_calendar_returns(monkeypatch) -> None:
    events: list[object] = []
    state = RecordingState(events)
    coordinator = RuntimeCoordinator(events)

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        state.trading_day_value = None
        state.calendar_alert_open = True
        coordinator.daily_report = None
        daily.func()
        state.trading_day_value = True
        state.calendar_alert_open = False
        coordinator.daily_report = SimpleNamespace(
            status="completed",
            business_date=NOW.date(),
        )
        daily.func()
        assert _last_runtime_state(events) == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, state=state, coordinator=coordinator).run()


def test_recovering_one_reason_does_not_hide_another_degraded_reason(
    monkeypatch,
) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(events)
    freshness_reports = deque(
        (
            SimpleNamespace(healthy=False, stale_products=("screener",), missing_products=()),
            SimpleNamespace(healthy=True, stale_products=(), missing_products=()),
        )
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        freshness = scheduler.get_job("snapshot-freshness")
        assert daily is not None
        assert freshness is not None
        coordinator.daily_report = SimpleNamespace(
            status="failed",
            business_date=NOW.date(),
        )
        daily.func()
        freshness.func()
        coordinator.daily_report = SimpleNamespace(
            status="completed",
            business_date=NOW.date(),
        )
        daily.func()
        assert _last_runtime_state(events) == "degraded"
        freshness.func()
        assert _last_runtime_state(events) == "running"

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(
        events,
        coordinator=coordinator,
        freshness_runner=lambda _now: freshness_reports.popleft(),
    ).run()


def test_startup_calendar_alert_logs_failure_without_completed(monkeypatch) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.calendar_alert_open = True
    coordinator = RuntimeCoordinator(
        events,
        startup_report=SimpleNamespace(
            attempted_dates=(),
            remaining_dates=(),
            reports=(),
        ),
    )

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        startup = scheduler.get_job("startup-catchup")
        assert startup is not None
        startup.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, state=state, coordinator=coordinator).run()

    logs = [event for event in events if isinstance(event, tuple) and event[0] == "log"]
    assert any(
        event[1:3] == ("scheduler.catchup.failed", "error")
        and event[3]["error_code"] == "TRADE_CALENDAR_UNAVAILABLE"
        for event in logs
    )
    assert not any(event[1] == "scheduler.catchup.completed" for event in logs)
    assert any(
        event[0] == "state.touch" and event[-1] == "degraded"
        for event in events
        if isinstance(event, tuple)
    )


def test_startup_calendar_alert_read_failure_is_isolated_and_degrades(
    monkeypatch,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    state.fail_alert_once = True
    coordinator = RuntimeCoordinator(events)

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        startup = scheduler.get_job("startup-catchup")
        assert startup is not None
        startup.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, state=state, coordinator=coordinator).run()

    logs = [event for event in events if isinstance(event, tuple) and event[0] == "log"]
    assert any(
        event[1:3] == ("scheduler.catchup.failed", "error")
        and event[3]["error_code"] == "RUNTIME_ERROR"
        for event in logs
    )
    assert not any(event[1] == "scheduler.catchup.completed" for event in logs)
    assert _last_runtime_state(events) == "degraded"


def test_daily_none_without_calendar_alert_logs_non_trading_day(
    monkeypatch,
) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(events)
    coordinator.daily_report = None

    def start_without_blocking(scheduler: BlockingScheduler) -> None:
        daily = scheduler.get_job("sync-daily")
        assert daily is not None
        daily.func()

    monkeypatch.setattr(BlockingScheduler, "start", start_without_blocking)
    _runtime(events, coordinator=coordinator).run()

    assert any(
        event[1:3] == ("scheduler.daily.skipped", "info")
        and event[3]["status"] == "non-trading-day"
        for event in events
        if isinstance(event, tuple) and event[0] == "log"
    )


def _last_runtime_state(events: list[object]) -> str:
    return next(
        event[-1]
        for event in reversed(events)
        if isinstance(event, tuple) and event[0] == "state.touch"
    )


def _database_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "macro_sync_enabled": False,
        "data_backend": "database",
        "database_url": "postgresql+psycopg://user:password@db.local/app",
        "tushare_token": SecretStr("token-secret"),
    }
    values.update(overrides)
    return Settings(**values)


def test_main_rejects_fixture_before_opening_runtime(monkeypatch, capsys) -> None:
    opened = False

    def unexpected_open(*_args: object, **_kwargs: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("fixture backend must fail before runtime assembly")

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: Settings(data_backend="fixture")),
    )
    monkeypatch.setattr(scheduler_module, "open_daily_sync_runtime", unexpected_open)

    assert scheduler_module.main() == 1
    assert opened is False
    assert "scheduler settings could not be loaded" in capsys.readouterr().err


def test_main_rejects_missing_custom_file_before_opening_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _database_settings(
        sync_universe="custom",
        sync_universe_file=tmp_path / "missing.json",
    )
    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must not be opened"),
    )

    assert scheduler_module.main() == 1


def test_main_rejects_missing_token_before_opening_runtime(monkeypatch) -> None:
    settings = Settings(
        data_backend="database",
        database_url="postgresql+psycopg://user:password@db.local/app",
    )
    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must not be opened"),
    )

    assert scheduler_module.main() == 1


def test_main_success_closes_runtime_and_returns_zero(monkeypatch) -> None:
    settings = _database_settings()
    lifecycle: list[str] = []
    runtime = DailySyncRuntime(
        job=object(),
        selection=UNIVERSE,
        mode=TushareMode.STANDARD,
        session_factory=object(),
    )

    class RuntimeContext:
        def __enter__(self) -> DailySyncRuntime:
            lifecycle.append("open")
            return runtime

        def __exit__(self, *_args: object) -> None:
            lifecycle.append("close")

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args: RuntimeContext(),
    )
    monkeypatch.setattr(
        SchedulerRuntime,
        "run",
        lambda _self: lifecycle.append("run"),
    )

    assert scheduler_module.main() == 0
    assert lifecycle == ["open", "run", "close"]


def test_main_assembles_once_and_releases_runtime_on_failure(
    monkeypatch,
    capsys,
) -> None:
    settings = _database_settings(sync_universe="all")
    lifecycle: list[str] = []
    runtime = DailySyncRuntime(
        job=object(),
        selection=UNIVERSE,
        mode=TushareMode.STANDARD,
        session_factory=object(),
    )

    class RuntimeContext:
        def __enter__(self) -> DailySyncRuntime:
            lifecycle.append("open")
            return runtime

        def __exit__(self, *_args: object) -> None:
            lifecycle.append("close")

    open_calls: list[tuple[object, ...]] = []

    def open_runtime(*args: object) -> RuntimeContext:
        open_calls.append(args)
        return RuntimeContext()

    def fail_run(self: SchedulerRuntime) -> None:
        lifecycle.append("run")
        raise RuntimeError(
            "provider token-secret at postgresql+psycopg://user:password@db.local/app"
        )

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(scheduler_module, "open_daily_sync_runtime", open_runtime)
    monkeypatch.setattr(SchedulerRuntime, "run", fail_run)

    assert scheduler_module.main(["--confirm-full-market"]) == 1

    captured = capsys.readouterr()
    assert lifecycle == ["open", "run", "close"]
    assert open_calls == [(settings, "all", None)]
    assert "token-secret" not in captured.err
    assert "password" not in captured.err
    assert settings.database_url not in captured.err
    assert "***" in captured.err


def test_settings_load_failure_emits_fixed_safe_startup_failure(
    monkeypatch,
    capsys,
) -> None:
    token = "pre-settings-token"
    database_url = "postgresql+psycopg://secret-user:secret-password@secret-host/secret-db"
    monkeypatch.setattr(scheduler_module, "configured_tushare_token", lambda: token)

    def fail_settings(_cls) -> Settings:
        raise ValueError(f"invalid {token} at {database_url}")

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(fail_settings),
    )

    assert scheduler_module.main() == 1

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "scheduler.startup.failed"
    assert payload["schedulerInstanceId"] is None
    assert payload["errorCode"] == "SCHEDULER_SETTINGS_INVALID"
    assert payload["message"] == "scheduler settings could not be loaded"
    combined = captured.out + captured.err
    for secret in (token, "secret-user", "secret-password", "secret-host", "secret-db"):
        assert secret not in combined
    assert captured.err.strip() == "scheduler settings could not be loaded"


def test_runtime_enter_failure_emits_structured_startup_failure(
    monkeypatch,
    capsys,
) -> None:
    settings = _database_settings()
    exit_called = False

    class FailingContext:
        def __enter__(self) -> DailySyncRuntime:
            raise RuntimeError(
                "database unavailable at " + (settings.database_url or "")
            )

        def __exit__(self, *_args: object) -> None:
            nonlocal exit_called
            exit_called = True

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args: FailingContext(),
    )

    assert scheduler_module.main() == 1

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "scheduler.startup.failed"
    assert payload["schedulerInstanceId"] is None
    assert payload["errorCode"] == "SCHEDULER_STARTUP_FAILED"
    assert settings.database_url not in captured.out + captured.err
    assert "password" not in captured.out + captured.err
    assert exit_called is False


def test_start_instance_failure_emits_safe_structured_startup_failure(
    monkeypatch,
    capsys,
) -> None:
    settings = _database_settings()
    runtime = DailySyncRuntime(
        job=object(),
        selection=UNIVERSE,
        mode=TushareMode.STANDARD,
        session_factory=object(),
    )

    class RuntimeContext:
        def __enter__(self) -> DailySyncRuntime:
            return runtime

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_start_instance(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError(
            "heartbeat database failed at " + (settings.database_url or "")
        )

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args: RuntimeContext(),
    )
    monkeypatch.setattr(
        SchedulerStateRepository,
        "start_instance",
        fail_start_instance,
    )

    assert scheduler_module.main() == 1

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "scheduler.startup.failed"
    assert payload["schedulerInstanceId"] is None
    assert payload["errorCode"] == "SCHEDULER_STARTUP_FAILED"
    assert settings.database_url not in captured.out + captured.err
    assert "password" not in captured.out + captured.err


def test_failure_logging_does_not_replace_runtime_primary_error(monkeypatch) -> None:
    events: list[object] = []
    coordinator = RuntimeCoordinator(events, fail_recovery=True)

    class FailingLogger(RecordingLogger):
        def emit(self, event: str, level: str, **fields: object) -> None:
            super().emit(event, level, **fields)
            if event == "scheduler.failed":
                raise BrokenPipeError("stdout unavailable")

    monkeypatch.setattr(
        BlockingScheduler,
        "start",
        lambda _scheduler: pytest.fail("scheduler must not start"),
    )

    with pytest.raises(RuntimeError, match="recovery failed"):
        _runtime(
            events,
            coordinator=coordinator,
            logger_factory=lambda _instance_id: FailingLogger(events),
        ).run()
    assert ("state.stop", "scheduler-1", NOW) in events


def test_runtime_context_cleanup_does_not_replace_primary_error(
    monkeypatch,
    capsys,
) -> None:
    settings = _database_settings()
    cleanup_attempted = False
    runtime = DailySyncRuntime(
        job=object(),
        selection=UNIVERSE,
        mode=TushareMode.STANDARD,
        session_factory=object(),
    )

    class FailingExitContext:
        def __enter__(self) -> DailySyncRuntime:
            return runtime

        def __exit__(self, *_args: object) -> None:
            nonlocal cleanup_attempted
            cleanup_attempted = True
            raise RuntimeError("cleanup failure")

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args: FailingExitContext(),
    )
    monkeypatch.setattr(
        SchedulerRuntime,
        "run",
        lambda _self: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )

    assert scheduler_module.main() == 1
    captured = capsys.readouterr()
    assert cleanup_attempted is True
    assert "primary failure" in captured.err
    assert "cleanup failure" not in captured.err


def test_runtime_context_cleanup_failure_is_reported_without_primary(
    monkeypatch,
    capsys,
) -> None:
    settings = _database_settings()
    runtime = DailySyncRuntime(
        job=object(),
        selection=UNIVERSE,
        mode=TushareMode.STANDARD,
        session_factory=object(),
    )

    class FailingExitContext:
        def __enter__(self) -> DailySyncRuntime:
            return runtime

        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("cleanup failure")

    monkeypatch.setattr(
        scheduler_module.Settings,
        "from_env",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        scheduler_module,
        "open_daily_sync_runtime",
        lambda *_args: FailingExitContext(),
    )
    monkeypatch.setattr(SchedulerRuntime, "run", lambda _self: None)

    assert scheduler_module.main() == 1
    assert "cleanup failure" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("selection", "configured_profile"),
    (
        (
            UniverseSelection(
                profile="representative-50",
                version="representative-v1",
                coverage="representative",
                symbols=("600519.SH",),
                fingerprint="representative-fingerprint",
            ),
            "representative-50",
        ),
        (
            UniverseSelection(
                profile="all",
                version="all-v1",
                coverage="full-market",
                symbols=("000001.SZ", "600519.SH"),
                fingerprint="all-fingerprint",
            ),
            "all",
        ),
        (
            UniverseSelection(
                profile="custom",
                version="custom-v1",
                coverage="custom",
                symbols=("000001.SZ",),
                fingerprint="custom-fingerprint",
            ),
            "custom",
        ),
    ),
)
def test_real_builder_wires_shared_selection_and_instance_id(
    monkeypatch,
    selection: UniverseSelection,
    configured_profile: str,
) -> None:
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        session.add(TradeCalendar(exchange="SSE", cal_date=NOW.date(), is_open=True))
    job = object()
    daily_runtime = DailySyncRuntime(
        job=job,
        selection=selection,
        mode=TushareMode.STANDARD,
        session_factory=factory,
    )
    settings = _database_settings(sync_universe=configured_profile)
    monkeypatch.setattr(BlockingScheduler, "start", lambda _scheduler: None)

    try:
        runtime = build_scheduler_runtime(settings, daily_runtime, clock=lambda: NOW)
        runtime.run()
    finally:
        engine.dispose()

    assert isinstance(runtime._state, SchedulerStateRepository)
    assert runtime._universe is selection
    assert isinstance(runtime._coordinator, ScheduledDailySyncCoordinator)
    assert runtime._coordinator._instance_id == runtime._instance_id
    assert runtime._coordinator._universe is selection
    assert runtime._coordinator._job is job
    assert runtime._coordinator._state is runtime._state
    assert isinstance(runtime._coordinator._resolver, ExpectedBusinessDateResolver)
    assert isinstance(runtime._coordinator._planner, CatchupPlanner)
    assert isinstance(runtime._coordinator._freshness, FreshnessService)


@pytest.mark.parametrize("stop_signal", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    "job_id", ("sync-daily", "startup-catchup", "snapshot-freshness", "scheduler-heartbeat"),
)
def test_job_boundary_propagates_process_control_signals(monkeypatch, job_id, stop_signal) -> None:
    events: list[object] = []
    state = RecordingState(events)
    coordinator = RuntimeCoordinator(events)
    signal = stop_signal("controlled stop")

    def interrupt(*_args):
        raise signal

    def start(scheduler):
        if job_id == "scheduler-heartbeat":
            monkeypatch.setattr(state, "heartbeat", interrupt)
        with pytest.raises(stop_signal) as caught:
            scheduler.get_job(job_id).func()
        assert caught.value is signal
        assert runtime._active_jobs == 0
        assert runtime._degraded_reasons == set()

    monkeypatch.setattr(coordinator, "run_scheduled", interrupt)
    monkeypatch.setattr(coordinator, "run_startup", interrupt)
    monkeypatch.setattr(BlockingScheduler, "start", start)
    runtime = _runtime(events, state=state, coordinator=coordinator, freshness_runner=interrupt)
    runtime.run()


@pytest.mark.parametrize("stop_signal", (KeyboardInterrupt, SystemExit))
def test_state_transition_propagates_process_control_without_primary_failure(
    monkeypatch, stop_signal,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    signal = stop_signal("controlled stop")

    def interrupt(*_args, **_kwargs):
        raise signal

    def start(scheduler):
        monkeypatch.setattr(state, "touch_instance", interrupt)
        with pytest.raises(stop_signal) as caught:
            runtime._set_degraded("daily", True)
        assert caught.value is signal

    monkeypatch.setattr(BlockingScheduler, "start", start)
    runtime = _runtime(events, state=state)
    runtime.run()


@pytest.mark.parametrize("job_id", ("sync-daily", "startup-catchup"))
def test_executor_boundary_never_logs_original_exception_or_chain(
    monkeypatch, capsys, caplog, job_id: str,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    coordinator = RuntimeCoordinator(events)
    secret = "invented-provider-token-49385"
    url = "postgresql://invented:fake-password@db.invalid/scheduler"
    password = "unconfigured-standalone-password-38521"

    def fail(_now):
        try:
            raise ValueError(f"{secret} {url}")
        except ValueError as cause:
            raise RuntimeError(f"provider failed: {secret} password={password}") from cause

    monkeypatch.setattr(coordinator, "run_scheduled", fail)
    monkeypatch.setattr(coordinator, "run_startup", fail)
    executor_logger = logging.getLogger("apscheduler.executors.default")
    handler = logging.StreamHandler(sys.stderr)
    executor_logger.addHandler(handler)

    def start(scheduler):
        job = scheduler.get_job(job_id)
        job.misfire_grace_time = None
        result = run_job(job, "default", [NOW], executor_logger.name)
        assert len(result) == 1
        failure = result[0]
        assert failure.code == EVENT_JOB_ERROR
        assert state.current_state == "degraded"
        # Exercise the same exception again through a normal traceback consumer.
        traceback.print_exception(failure.exception)
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in caplog.text
        assert secret not in failure.traceback
        assert url not in captured.out + captured.err + caplog.text + failure.traceback
        assert password not in captured.out + captured.err + caplog.text + failure.traceback
        assert isinstance(failure.exception, scheduler_module.SchedulerJobError)
        assert failure.exception.__cause__ is None
        assert failure.exception.__context__ is None
        assert failure.exception.error_code == "RUNTIME_ERROR"
        payloads = [json.loads(line) for line in captured.out.splitlines()]
        assert any(item.get("errorCode") == "RUNTIME_ERROR" for item in payloads)
        assert any(record.name == executor_logger.name and record.exc_info for record in caplog.records)

    monkeypatch.setattr(BlockingScheduler, "start", start)
    try:
        _runtime(
            events, state=state, coordinator=coordinator,
            logger_factory=lambda instance_id: SchedulerJsonLogger(
                instance_id,
                sanitize=lambda value: value.replace(secret, "***").replace(url, "***"),
            ),
        ).run()
    finally:
        executor_logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("gaps_remain,other_failure", ((False, False), (True, False), (False, True)))
def test_periodic_production_refresh_reconciles_backlog_without_restarting(
    monkeypatch, gaps_remain: bool, other_failure: bool,
) -> None:
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    PublishedSnapshot.__table__.create(engine)
    state = SchedulerStateRepository(factory)
    state.ensure_checkpoint(UNIVERSE, fallback=NOW.date(), now=NOW)
    with factory.begin() as session:
        session.add(TradeCalendar(exchange="SSE", cal_date=NOW.date(), is_open=True))
        for product in ("market-overview", "market-explorer", "screener", "portfolio"):
            session.add(PublishedSnapshot(
                product=product, version="v1", as_of=NOW, payload={},
                stale=False, published_at=NOW,
            ))
    daily_runtime = DailySyncRuntime(
        job=object(), selection=UNIVERSE, mode=TushareMode.STANDARD, session_factory=factory,
    )
    runtime = build_scheduler_runtime(_database_settings(), daily_runtime, clock=lambda: NOW)
    key = "scheduler:catchup-backlog:custom:custom-v1"
    original = state.open_alert(key, "catchup-backlog", "warning", NOW, {"remainingCount": 1})

    def start(scheduler):
        runtime._set_degraded("catchup", True)
        if other_failure:
            runtime._set_degraded("daily", True)
        assert state.heartbeat(runtime._instance_id).state == "degraded"
        # An independent completion, with no second startup invocation.
        if not gaps_remain:
            with factory.begin() as session:
                session.add(JobRun(
                    id="manual-completion", job_name="sync-daily",
                    business_key=daily_sync_business_key(NOW.date(), UNIVERSE),
                    status="completed", started_at=NOW, finished_at=NOW, details={"trigger": "cli"},
                ))
        scheduler.get_job("snapshot-freshness").func()
        scheduler.get_job("scheduler-heartbeat").func()
        alert = state.alert(key)
        assert alert.id == original.id
        assert alert.status == ("open" if gaps_remain else "resolved")
        assert ("catchup" in runtime._degraded_reasons) is gaps_remain
        assert state.heartbeat(runtime._instance_id).state == (
            "degraded" if gaps_remain or other_failure else "running"
        )

    monkeypatch.setattr(BlockingScheduler, "start", start)
    try:
        runtime.run()
    finally:
        engine.dispose()


def test_shutdown_drains_workers_before_runtime_exit_and_rejects_new_work(monkeypatch) -> None:
    events: list[object] = []
    entered = Event()
    release = Event()
    finished = Event()
    shutdown_requested = Event()
    coordinator = RuntimeCoordinator(events)
    pool = ThreadPoolExecutor(max_workers=1)
    scheduler_holder = []
    workers = []
    outcomes = []

    def work(now):
        entered.set()
        assert release.wait(timeout=5)
        # Real get_job acquires the APScheduler job store lock.
        assert scheduler_holder[0].get_job("sync-daily") is not None
        finished.set()
        return SimpleNamespace(status="completed", business_date=now.date())

    monkeypatch.setattr(coordinator, "run_scheduled", work)
    original_pause = BlockingScheduler.pause

    def pause(scheduler):
        original_pause(scheduler)
        shutdown_requested.set()

    def start(scheduler):
        scheduler_holder.append(scheduler)
        scheduler._event = Event()
        BaseScheduler.start(scheduler, paused=True)
        workers.append(pool.submit(scheduler.get_job("sync-daily").func))
        assert entered.wait(timeout=5)
        raise KeyboardInterrupt

    def shutdown(scheduler, *, wait):
        scheduler.state = 0
        shutdown_requested.set()
        pool.shutdown(wait=wait)

    def drive():
        try:
            runtime.run()
            outcomes.append(("runtime.exit", finished.is_set()))
        except BaseException as error:
            outcomes.append(error)

    monkeypatch.setattr(BlockingScheduler, "start", start)
    monkeypatch.setattr(BlockingScheduler, "pause", pause)
    monkeypatch.setattr(BlockingScheduler, "shutdown", shutdown)
    runtime = _runtime(events, coordinator=coordinator)
    driver = Thread(target=drive)
    driver.start()
    try:
        assert shutdown_requested.wait(timeout=5)
        scheduler_holder[0].get_job("startup-catchup").func()
        assert not any(isinstance(item, tuple) and item[0] == "coordinator.startup" for item in events)
        assert not any(isinstance(item, tuple) and item[0] == "state.stop" for item in events)
        assert outcomes == []
    finally:
        release.set()
        driver.join(timeout=5)
        pool.shutdown(wait=True)
    assert not driver.is_alive()
    workers[0].result(timeout=1)
    assert outcomes == [("runtime.exit", True)]
    assert events[-1] == ("state.stop", "scheduler-1", NOW)


@pytest.mark.parametrize(
    "job_id", ("sync-daily", "startup-catchup", "snapshot-freshness", "scheduler-heartbeat"),
)
@pytest.mark.parametrize("logging_error", (BrokenPipeError, SystemExit))
@pytest.mark.parametrize("state_write_fails", (False, True))
def test_executor_preserves_primary_when_failure_logger_raises(
    monkeypatch, capsys, caplog, job_id, logging_error, state_write_fails,
) -> None:
    events: list[object] = []
    state = RecordingState(events)
    coordinator = RuntimeCoordinator(events)
    secret = "invented-primary-token-65281"
    url = "postgresql://invented:secret-password@db.invalid/example"
    failure_fields = []
    original_touch = state.touch_instance

    def touch(instance_id, **kwargs):
        if state_write_fails and kwargs["state"] == "degraded":
            raise SystemExit(url)
        return original_touch(instance_id, **kwargs)

    monkeypatch.setattr(state, "touch_instance", touch)

    def fail(*_args):
        try:
            raise ValueError(url)
        except ValueError as cause:
            raise RuntimeError(secret) from cause

    class FailingLogger(RecordingLogger):
        def emit(self, event, level, **fields):
            if level == "error":
                failure_fields.append(fields)
                raise logging_error(f"logging failed: {secret} {url}")
            super().emit(event, level, **fields)

    def start(scheduler):
        if job_id == "scheduler-heartbeat":
            monkeypatch.setattr(state, "heartbeat", fail)
        job = scheduler.get_job(job_id)
        job.misfire_grace_time = None
        failure = run_job(job, "default", [NOW], "apscheduler.executors.default")[0]
        assert failure.code == EVENT_JOB_ERROR
        assert isinstance(failure.exception, scheduler_module.SchedulerJobError)
        assert failure.exception.error_code == "RUNTIME_ERROR"
        assert failure_fields[0]["error_code"] == "RUNTIME_ERROR"
        assert runtime._operational_state == "degraded"
        assert failure.exception.__cause__ is None
        assert failure.exception.__context__ is None
        traceback.print_exception(failure.exception)
        captured = capsys.readouterr()
        for value in (secret, url):
            assert value not in captured.out + captured.err + caplog.text + failure.traceback

    monkeypatch.setattr(coordinator, "run_scheduled", fail)
    monkeypatch.setattr(coordinator, "run_startup", fail)
    monkeypatch.setattr(BlockingScheduler, "start", start)
    runtime = _runtime(
        events, state=state, coordinator=coordinator, freshness_runner=fail,
        logger_factory=lambda _instance_id: FailingLogger(events),
    )
    runtime.run()


@pytest.mark.parametrize("job_kind", ("daily", "macro"))
@pytest.mark.parametrize("pause_fails", (False, True))
def test_real_executor_drains_and_closes_resources_after_jobs(monkeypatch, pause_fails, job_kind) -> None:
    events: list[object] = []
    entered, release, finished = Event(), Event(), Event()
    paused, queued, heartbeat_completed = Event(), Event(), Event()
    coordinator = RuntimeCoordinator(events)
    scheduler_holder = []
    job_events = []
    outcomes = []
    shutdown_waits = []

    def work(now):
        entered.set()
        assert release.wait(timeout=5)
        scheduler = scheduler_holder[0]
        # A bounded lock acquisition makes a shutdown/get_job deadlock fail
        # deterministically instead of hanging the test process.
        assert scheduler._jobstores_lock.acquire(timeout=2)
        try:
            assert scheduler.get_job("sync-daily") is not None
        finally:
            scheduler._jobstores_lock.release()
        finished.set()
        return SimpleNamespace(status="completed", business_date=now.date())

    def completed(event):
        job_events.append(event)
        if event.job_id == "scheduler-heartbeat":
            heartbeat_completed.set()

    def start(scheduler):
        scheduler_holder.append(scheduler)
        scheduler._event = Event()
        BaseScheduler.start(scheduler, paused=True)
        scheduler.add_listener(completed, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        executor = scheduler._executors["default"]
        daily = scheduler.get_job("sync-daily" if job_kind == "daily" else "sync-macro")
        daily.misfire_grace_time = None
        executor.submit_job(daily, [NOW])
        assert entered.wait(timeout=5)
        heartbeat = scheduler.get_job("scheduler-heartbeat")
        heartbeat.misfire_grace_time = None
        executor.submit_job(heartbeat, [NOW])
        assert heartbeat_completed.wait(timeout=5)
        startup = scheduler.get_job("startup-catchup")
        guarded = startup.func

        def queued_startup():
            queued.set()
            return guarded()

        startup.modify(func=queued_startup)
        executor.submit_job(startup, [NOW])
        assert queued.wait(timeout=5)
        raise KeyboardInterrupt

    original_pause = BlockingScheduler.pause
    original_shutdown = BlockingScheduler.shutdown

    def pause(scheduler):
        paused.set()
        if pause_fails:
            raise RuntimeError("pause failed")
        original_pause(scheduler)

    def shutdown(scheduler, *, wait):
        shutdown_waits.append(wait)
        original_shutdown(scheduler, wait=wait)

    class RuntimeContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            assert finished.is_set()
            assert events[-1] == ("state.stop", "scheduler-1", NOW)
            assert len(job_events) == 3
            events.append("resources.closed")

    monkeypatch.setattr(coordinator, "run_scheduled", work)
    monkeypatch.setattr(BlockingScheduler, "start", start)
    monkeypatch.setattr(BlockingScheduler, "pause", pause)
    monkeypatch.setattr(BlockingScheduler, "shutdown", shutdown)
    monkeypatch.setattr(Settings, "from_env", lambda: _database_settings())
    monkeypatch.setattr(scheduler_module, "open_daily_sync_runtime", lambda *_args: RuntimeContext())
    runtime = _runtime(events, coordinator=coordinator)
    if job_kind == "macro":
        runtime._macro_coordinator = SimpleNamespace(run_scheduled=work)
    monkeypatch.setattr(scheduler_module, "build_scheduler_runtime", lambda *_args: runtime)

    def drive():
        try:
            outcomes.append(scheduler_module.main())
        except BaseException as error:
            outcomes.append(error)

    driver = Thread(target=drive)
    driver.start()
    try:
        assert paused.wait(timeout=5)
        assert outcomes == []
        assert "resources.closed" not in events
        assert not any(isinstance(item, tuple) and item[0] == "state.stop" for item in events)
    finally:
        release.set()
        driver.join(timeout=5)
    assert not driver.is_alive()
    assert outcomes == [0]
    assert shutdown_waits == [True]
    assert all(event.code == EVENT_JOB_EXECUTED for event in job_events)
    assert not any(isinstance(item, tuple) and item[0] == "coordinator.startup" for item in events)
    assert events[-1] == "resources.closed"


def test_macro_startup_failure_does_not_mask_primary_daily_error(monkeypatch):
    events = []
    runtime = _runtime(events)
    coordinator = RuntimeCoordinator(events)
    def fail_daily(now):
        raise ValueError("daily error")
    def fail_macro(now):
        raise RuntimeError("macro error")
    coordinator.run_startup = fail_daily
    runtime._coordinator_factory = lambda _: coordinator
    runtime._macro_coordinator = SimpleNamespace(run_startup=fail_macro, run_scheduled=fail_macro)
    def start(scheduler):
        with pytest.raises(scheduler_module.SchedulerJobError) as error:
            scheduler.get_job("startup-catchup").func()
        assert error.value.error_code == "VALUE_ERROR"
        assert {"macro", "catchup"} <= runtime._degraded_reasons
    monkeypatch.setattr(BlockingScheduler, "start", start)
    runtime.run()


@pytest.mark.parametrize("enabled", [False, True])
def test_production_builder_opens_shared_macro_runtime_lazily(monkeypatch, enabled):
    from contextlib import contextmanager
    from app.macro.jobs.macro_sync import MacroSyncReport
    from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        session.add(TradeCalendar(exchange="SSE", cal_date=NOW.date(), is_open=True))
    calls = []
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    class MacroJob:
        def run(self, options, *, on_completed):
            calls.append(options)
            return on_completed(MacroSyncReport("completed", "run-1", "key", 1, options.as_of, catalog.catalog_version))
    @contextmanager
    def open_runtime(settings):
        calls.append("opened")
        try:
            yield MacroSyncRuntime(MacroJob(), catalog)
        finally:
            calls.append("closed")
    monkeypatch.setattr(scheduler_module, "open_macro_sync_runtime", open_runtime)
    settings = _database_settings(macro_sync_enabled=enabled)
    daily = DailySyncRuntime(job=object(), selection=UNIVERSE, mode=TushareMode.STANDARD, session_factory=factory)
    runtime = build_scheduler_runtime(settings, daily, clock=lambda: NOW)
    assert calls == []
    def start(scheduler):
        macro = scheduler.get_job("sync-macro")
        if enabled:
            macro.func()
            assert calls[0] == "opened" and calls[-1] == "closed"
            assert calls[1].mode == "live" and calls[1].trigger == "scheduler"
        else:
            assert macro is None and calls == []
    monkeypatch.setattr(BlockingScheduler, "start", start)
    try:
        runtime.run()
    finally:
        engine.dispose()
