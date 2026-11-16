from contextlib import contextmanager
from datetime import UTC, datetime, time

import pytest

from app.config import Settings
from app.macro.jobs import macro_scheduler
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync import MacroSyncReport
from app.macro.jobs.macro_sync_history import MacroCatalogVersionHashMismatch, SqlMacroSyncHistory
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
from app.jobs.scheduler_state import SchedulerStateRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


@pytest.mark.parametrize(("now", "weekday", "hour", "zone", "expected"), [
    ("2026-09-21T00:59:00+00:00", "mon", "09:00", "Asia/Shanghai", "2026-09-14T01:00:00+00:00"),
    ("2026-09-21T01:00:00+00:00", "mon", "09:00", "Asia/Shanghai", "2026-09-21T01:00:00+00:00"),
    ("2026-09-21T02:00:00+00:00", "mon", "09:00", "Asia/Shanghai", "2026-09-21T01:00:00+00:00"),
    ("2026-09-20T12:00:00+00:00", "mon", "09:00", "Asia/Shanghai", "2026-09-14T01:00:00+00:00"),
    ("2026-09-23T02:00:00+00:00", "wed", "09:00", "Asia/Shanghai", "2026-09-23T01:00:00+00:00"),
    ("2026-03-09T12:59:00+00:00", "mon", "09:00", "America/New_York", "2026-03-02T14:00:00+00:00"),
    ("2026-03-09T13:00:00+00:00", "mon", "09:00", "America/New_York", "2026-03-09T13:00:00+00:00"),
    ("2026-11-02T14:00:00+00:00", "mon", "09:00", "America/New_York", "2026-11-02T14:00:00+00:00"),
    ("2026-03-08T07:30:00+00:00", "sun", "02:30", "America/New_York", "2026-03-08T07:30:00+00:00"),
    ("2026-11-01T05:45:00+00:00", "sun", "01:30", "America/New_York", "2026-11-01T05:30:00+00:00"),
])
def test_latest_slot(now, weekday, hour, zone, expected):
    result = macro_scheduler.most_recent_macro_schedule(
        datetime.fromisoformat(now), weekday, time.fromisoformat(hour), zone,
    )
    assert result.astimezone(UTC) == datetime.fromisoformat(expected)


def test_latest_slot_requires_aware_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        macro_scheduler.most_recent_macro_schedule(datetime(2026, 9, 21), "mon", time(9), "Asia/Shanghai")


@pytest.fixture
def harness(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    history = SqlMacroSyncHistory(factory)
    state = SchedulerStateRepository(factory)
    calls = []
    now = datetime.fromisoformat("2026-09-27T12:00:00+00:00")
    status = ["partial"]

    class Job:
        def run(self, options, *, on_completed):
            lock = PostgresAdvisoryJobLock(engine)
            if not lock.try_acquire(data_publication_lock_key()):
                return MacroSyncReport("locked", "run-1", "key", 1, options.as_of, catalog.catalog_version)
            try:
                calls.append(options)
                return on_completed(MacroSyncReport(status[0], "run-1", "key", 1, options.as_of, catalog.catalog_version))
            finally:
                lock.release(data_publication_lock_key())

    @contextmanager
    def runtime():
        yield MacroSyncRuntime(Job(), catalog)

    def coordinator(**overrides):
        return macro_scheduler.MacroScheduledCoordinator(
            settings=Settings(**overrides), catalog=catalog, history=history,
            state=state, runtime_factory=runtime, publication_lock=PostgresAdvisoryJobLock(engine),
            clock=lambda: now,
        )

    yield coordinator, calls, history, state, catalog, now, status
    engine.dispose()


def test_startup_attempts_only_latest_slot_once_even_after_weeks_offline(harness):
    make, calls, history, _, catalog, now, _ = harness
    old = history.start("2026-W20:" + catalog.catalog_version, now, catalog.catalog_version, catalog.catalog_hash, "scheduler", 1)
    history.finish(old, "failed", {})
    coordinator = make()
    assert coordinator.run_startup(now).status == "partial"
    assert coordinator.run_startup(now) is None
    assert len(calls) == 1
    assert calls[0].as_of == datetime.fromisoformat("2026-09-21T01:00:00+00:00")
    assert calls[0].trigger == "scheduler" and calls[0].mode == "live"
    assert calls[0].history_years == 10 and calls[0].force is False
    assert calls[0].catalog_hash == catalog.catalog_hash
    assert make().run_startup(now).status == "partial"
    assert len(calls) == 2


@pytest.mark.parametrize("settings", [{"macro_sync_enabled": False}, {"macro_sync_catchup_on_start": False}])
def test_startup_disabled_never_opens_runtime(harness, settings):
    make, calls, _, _, _, now, _ = harness
    assert make(**settings).run_startup(now) is None
    assert calls == []


def test_completed_latest_slot_skips_but_hash_mismatch_still_fails(harness):
    make, calls, history, _, catalog, now, _ = harness
    key = "2026-W39:" + catalog.catalog_version
    run = history.start(key, now, catalog.catalog_version, catalog.catalog_hash, "scheduler", 1)
    history.finish(run, "completed", {})
    assert make().run_startup(now) is None
    assert calls == []
    other = history.start(key, now, catalog.catalog_version, "b" * 64, "cli", 2)
    history.finish(other, "partial", {})
    with pytest.raises(MacroCatalogVersionHashMismatch):
        make().run_startup(now)
    assert calls == []


def test_business_key_uses_configured_timezone_and_weekday(harness):
    make, calls, history, _, catalog, _, _ = harness
    now = datetime.fromisoformat("2026-09-21T00:45:00+00:00")
    run = history.start("2026-W39:" + catalog.catalog_version, now, catalog.catalog_version, catalog.catalog_hash, "cli", 1)
    history.finish(run, "completed", {})
    assert make(macro_sync_time=time(8, 30)).run_startup(now) is None
    assert calls == []


def test_health_alert_transitions_and_scheduled_live_options(harness):
    make, calls, _, state, _, now, status = harness
    for value, expected in [("partial", "open"), ("locked", "open"), ("skipped", "open"), ("failed", "open"), ("completed", "resolved"), ("locked", "resolved")]:
        coordinator = make()
        status[0] = value
        assert coordinator.run_scheduled(now).status == value
        alert = state.alert("macro-sync:degraded")
        assert alert.status == expected
        if value == "failed":
            assert alert.severity == "error"
        if value == "partial":
            assert alert.severity == "warning"
    assert all(item.trigger == "scheduler" and item.mode == "live" for item in calls)


def test_publication_contention_skips_runtime_and_preserves_alert(pg_factory, harness):
    make, calls, _, state, _, now, _ = harness
    lock = PostgresAdvisoryJobLock(pg_factory.kw["bind"])
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert make().run_startup(now).status == "locked"
        assert calls == [] and state.alert("macro-sync:degraded") is None
    finally:
        lock.release(data_publication_lock_key())
    assert make().run_startup(now).status == "partial"


def test_health_is_recorded_before_releasing_cross_process_lock(pg_factory, harness, monkeypatch):
    make, _, _, state, _, now, _ = harness
    original = state.open_alert
    def record(*args, **kwargs):
        contender = PostgresAdvisoryJobLock(pg_factory.kw["bind"])
        acquired = contender.try_acquire(data_publication_lock_key())
        if acquired:
            contender.release(data_publication_lock_key())
        assert acquired is False
        return original(*args, **kwargs)
    monkeypatch.setattr(state, "open_alert", record)
    assert make().run_startup(now).status == "partial"


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_duplicate_fold_fire_does_not_retry_in_same_process(harness, status):
    make, calls, _, _, _, _, outcome = harness
    outcome[0] = status
    coordinator = make(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time="01:30")
    first = datetime.fromisoformat("2026-11-01T05:30:00+00:00")
    duplicate = datetime.fromisoformat("2026-11-01T06:30:00+00:00")
    assert coordinator.run_scheduled(first).status == status
    assert coordinator.run_scheduled(duplicate) is None
    assert len(calls) == 1 and calls[0].as_of == first
    assert coordinator.run_scheduled(datetime.fromisoformat("2026-11-08T06:30:00+00:00")).status == status
    assert len(calls) == 2
    assert make(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time="01:30").run_startup(duplicate).status == status


@pytest.mark.parametrize("status", ["partial", "failed", "completed"])
def test_runtime_close_failure_still_records_health_under_lock(pg_factory, harness, monkeypatch, status):
    make, _, history, state, catalog, now, _ = harness
    state.open_alert("macro-sync:degraded", "macro_sync", "error", now, {})
    state.resolve_alert("macro-sync:degraded", now)
    if status == "completed":
        state.open_alert("macro-sync:degraded", "macro_sync", "error", now, {})
    events = []
    expected = "resolved" if status == "completed" else "open"
    failure = RuntimeError("controlled close failure")
    class Job:
        def run(self, options, *, on_completed):
            lock = PostgresAdvisoryJobLock(pg_factory.kw["bind"])
            assert lock.try_acquire(data_publication_lock_key())
            try:
                key = "2026-W39:" + catalog.catalog_version
                run_id = history.start(key, options.as_of, catalog.catalog_version, catalog.catalog_hash, "scheduler", 1)
                history.finish(run_id, status, {})
                return on_completed(MacroSyncReport(status, run_id, key, 1, options.as_of, catalog.catalog_version))
            finally:
                lock.release(data_publication_lock_key())
    @contextmanager
    def runtime():
        yield MacroSyncRuntime(Job(), catalog)
        events.append("closing")
        raise failure
    coordinator = make()
    coordinator._runtime_factory = runtime
    method = "resolve_alert" if status == "completed" else "open_alert"
    original = getattr(state, method)
    def record(*args, **kwargs):
        contender = PostgresAdvisoryJobLock(pg_factory.kw["bind"])
        acquired = contender.try_acquire(data_publication_lock_key())
        if acquired:
            contender.release(data_publication_lock_key())
        assert acquired is False
        events.append("health")
        return original(*args, **kwargs)
    monkeypatch.setattr(state, method, record)
    with pytest.raises(RuntimeError) as caught:
        coordinator.run_startup(now)
    assert caught.value is failure
    assert state.alert("macro-sync:degraded").status == expected
    assert events == ["health", "closing"]
    contender = PostgresAdvisoryJobLock(pg_factory.kw["bind"])
    assert contender.try_acquire(data_publication_lock_key())
    contender.release(data_publication_lock_key())
    if status == "completed":
        assert make().run_startup(now) is None
        assert state.alert("macro-sync:degraded").status == "resolved"
