from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from threading import Event, Thread
import time

from apscheduler.schedulers.background import BackgroundScheduler
from app.config import Settings
from app.jobs.scheduler_control import ManagedSchedule, summarize
from app.local_admin.configuration import TASKS
from app.local_admin.control import ControlStore


def until(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(.02)


def test_only_enabled_generation_admitted(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    for name in TASKS:
        store.target(name, name == "macro", "daily")
    started, release = Event(), Event()
    calls = []
    scheduler = BackgroundScheduler(executors={"control": {"type": "threadpool", "max_workers": 1}})
    def run(*, startup=False):
        calls.append(startup)
        started.set()
        release.wait(6)
        return {"status": "completed"}
    runtime = SimpleNamespace(_scheduler=scheduler, _settings=Settings(), _now=lambda:datetime.now(UTC),
                              _run_macro=run, _guard_job=lambda fn, reason:fn)
    made = []
    def make_macro(settings):
        assert settings.macro_sync_enabled
        made.append(1)
        return object()
    control = ManagedSchedule(store, runtime, make_macro)
    control.install()
    scheduler.start()
    try:
        assert started.wait(5)
        old = scheduler.get_job("sync-macro").func
        store.target("macro", False, "daily")
        until(lambda:scheduler.get_job("sync-macro") is None)
        assert store.get("macro")["active"]
        release.set()
        until(lambda:not store.get("macro")["active"])
        old()
        assert calls == [True]
        store.target("macro", True, "daily")
        until(lambda:len(calls)==2)
        control.tick()
        assert calls == [True,True] and len(made)==2
    finally:
        release.set()
        scheduler.shutdown()


def test_recovery_reports_gaps_and_limited_sources():
    result = SimpleNamespace(remaining_dates=("2026-11-12",), attempted_dates=("2026-11-11",))
    assert "仍有积压" in summarize("stocks", result, True)
    assert "覆盖待核对" in summarize("market", {"status": "completed"}, True)
