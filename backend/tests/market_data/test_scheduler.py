import importlib
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import Settings
from app.jobs.scheduler import configure_scheduler
from backend.tests.market_data.test_catalog import catalog
from backend.tests.market_data.test_repository import NOW
from backend.tests.jobs.test_scheduler import _runtime, RuntimeCoordinator


def module():
    assert importlib.util.find_spec('app.market.jobs.market_scheduler') is not None, 'market scheduled coordinator missing'
    return importlib.import_module('app.market.jobs.market_scheduler')


def test_market_daily_job_keeps_existing_stock_and_weekly_macro_triggers():
    value = module()
    scheduler = BlockingScheduler(timezone='Asia/Shanghai')
    settings = Settings(routine_market_enabled=True)
    coordinator = SimpleNamespace(run_scheduled=lambda now:None, run_startup=lambda now:None)
    configure_scheduler(scheduler, settings, coordinator, clock=lambda:NOW, macro_job=lambda:None,
        market_job=lambda:None, market_schedule=catalog().schedule)
    market = scheduler.get_job('sync-market-data')
    assert market is not None and market.max_instances == 1 and market.coalesce is True
    assert market.trigger.get_next_fire_time(None, NOW).isoformat() == '2026-10-16T11:00:00+00:00'
    assert scheduler.get_job('sync-daily') is not None
    assert scheduler.get_job('sync-macro') is not None
    assert 'mon' in str(scheduler.get_job('sync-macro').trigger)


def test_market_coordinator_uses_same_service_and_can_retry_after_failure():
    value = module()
    attempts = []
    def runner(settings, *, now, dry_run, publish):
        attempts.append((now,dry_run,publish))
        return {'status': 'failed' if len(attempts) == 1 else 'completed', 'requestCount': 1}
    coordinator = value.MarketScheduledCoordinator(Settings(routine_market_enabled=True), runner=runner)
    assert coordinator.run_startup(NOW)['status'] == 'failed'
    assert coordinator.run_scheduled(NOW)['status'] == 'completed'
    assert attempts == [(NOW,False,True)] * 2


@pytest.mark.parametrize('failure', ['daily','market','macro'])
def test_each_startup_group_runs_despite_other_group_failure(monkeypatch, failure):
    value = module()
    events, attempts = [], []
    runtime = _runtime(events)
    def run(name):
        attempts.append(name)
        if name == failure:
            raise ValueError('controlled')
        return None
    monkeypatch.setattr(runtime, '_run_startup', lambda:run('daily'))
    monkeypatch.setattr(runtime, '_run_market_startup', lambda:run('market'))
    monkeypatch.setattr(runtime, '_run_macro_startup', lambda:run('macro'))
    with pytest.raises(ValueError):
        runtime._run_startup_jobs()
    assert attempts == ['daily','market','macro']


def test_disabled_market_does_not_load_catalog_or_register_job(monkeypatch):
    value = module()
    def forbidden():
        raise AssertionError('catalog must not load')
    monkeypatch.setattr(value.MarketDataCatalog, 'load', forbidden)
    coordinator = value.MarketScheduledCoordinator(Settings(routine_market_enabled=False))
    assert coordinator.run_scheduled(NOW) is None


def test_runtime_market_job_is_lifecycle_guarded_and_heartbeat_independent(monkeypatch):
    value = module()
    events, calls = [], []
    runtime = _runtime(events)
    runtime._settings = runtime._settings.model_copy(update={'routine_market_enabled':True})
    runtime._market_coordinator = SimpleNamespace(catalog=catalog(), run_scheduled=lambda now:calls.append(now) or {'status':'completed'}, run_startup=lambda now:None)
    def start(scheduler):
        job = scheduler.get_job('sync-market-data')
        assert job is not None
        job.func()
        assert runtime._active_jobs == 0
        scheduler.get_job('scheduler-heartbeat').func()
    monkeypatch.setattr(BlockingScheduler, 'start', start)
    runtime.run()
    assert len(calls) == 1


def test_actual_startup_path_paces_each_supplier_group(monkeypatch):
    from app.jobs.supplier_job_pacing import SupplierJobPacer
    runtime = _runtime([])
    runtime._settings = runtime._settings.model_copy(update={'routine_market_enabled':True})
    clock, observed = [0.0], []
    def sleep(seconds):
        clock[0] += seconds
    runtime._supplier_pacer = SupplierJobPacer(60,clock=lambda:clock[0],sleep=sleep)
    monkeypatch.setattr(runtime,'_run_startup',lambda:observed.append(clock[0]))
    monkeypatch.setattr(runtime,'_run_market',lambda **kw:observed.append(clock[0]))
    monkeypatch.setattr(runtime,'_run_macro',lambda **kw:observed.append(clock[0]))
    runtime._market_coordinator = object()
    runtime._macro_coordinator = object()
    runtime._guard_job(runtime._run_startup_jobs,'catchup')()
    assert observed == [0.0,1.0,2.0]
