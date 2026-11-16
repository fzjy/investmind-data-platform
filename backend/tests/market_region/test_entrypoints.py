import importlib
from contextlib import contextmanager
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler
from typer.testing import CliRunner

from app.config import Settings
from app.cli.main import app
from app.market.data.catalog import MarketDataCatalog
from app.jobs.scheduler import configure_scheduler
from backend.tests.market_region.test_sync import setup, NOW, POOL
from backend.tests.jobs.test_scheduler import _runtime


def test_cli_default_is_readonly_and_explicit_publish(monkeypatch):
    assert importlib.util.find_spec('app.cli.security_region') is not None, 'region CLI missing'
    module=importlib.import_module('app.cli.security_region')
    calls=[]
    def run(settings,*,now,dry_run,publish):
        calls.append((dry_run,publish))
        return {'status':'planned' if dry_run else 'completed','requestCount':0}
    monkeypatch.setattr(module,'run_region_update',run)
    monkeypatch.setattr(Settings,'from_env',lambda:Settings())
    for args in ([],['--publish'],['--dry-run','--publish']):
        assert CliRunner().invoke(app,['sync-security-region',*args]).exit_code==0
    assert calls==[(True,False),(False,True),(True,True)]


def test_only_region_group_total_and_failure_does_not_skip_macro(monkeypatch):
    from app.jobs import routine_update as module
    assert 'routine_region_enabled' in Settings.model_fields, 'region flag missing'
    calls=[]
    monkeypatch.setattr(module,'run_region_step',lambda *a,**kw:calls.append('region') or {
        'status':'completed','publishReady':True,'requestCount':2,'publication':{'status':'no_change'}})
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,
        routine_macro_enabled=False,routine_region_enabled=True)
    result=module.run_routine_update(settings,now=NOW,publish=True,macro_rebuild=lambda _:None)
    assert result['status']=='completed' and result['httpCounts']=={'stocks':0,'macro':0,'region':2,'total':2}
    assert calls==['region']
    def fail(*a,**kw): raise RuntimeError('secret')
    monkeypatch.setattr(module,'run_region_step',fail)
    monkeypatch.setattr(module,'run_macro_catchup',lambda *a,**kw:calls.append('macro') or {'status':'completed','activeCoverageCompleted':True,'requestCount':0})
    result=module.run_routine_update(settings.model_copy(update={'routine_macro_enabled':True}),now=NOW,macro_rebuild=lambda _:None)
    assert result['status']=='failed' and result['httpCounts']['total'] is None
    assert calls==['region','macro'] and 'secret' not in str(result)


def test_scheduler_region_daily_and_lifecycle_guard(monkeypatch):
    assert 'routine_region_enabled' in Settings.model_fields, 'region flag missing'
    scheduler=BlockingScheduler(timezone='Asia/Shanghai')
    configure_scheduler(scheduler,Settings(routine_region_enabled=True),
        SimpleNamespace(run_scheduled=lambda _:None,run_startup=lambda _:None),clock=lambda:NOW,
        region_job=lambda:None,region_schedule=MarketDataCatalog.load().schedule)
    job=scheduler.get_job('sync-security-region')
    assert job.max_instances==1 and job.coalesce
    assert job.trigger.get_next_fire_time(None,NOW).isoformat()=='2026-10-17T11:00:00+00:00'
    runtime=_runtime([])
    assert hasattr(runtime,'_region_coordinator')
    runtime._settings=runtime._settings.model_copy(update={'routine_region_enabled':True})
    calls=[]
    runtime._region_coordinator=SimpleNamespace(catalog=MarketDataCatalog.load(),
        run_scheduled=lambda now:calls.append('scheduled') or {'status':'completed'},
        run_startup=lambda now:calls.append('startup') or {'status':'completed'})
    def start(scheduler):
        scheduler.get_job('sync-security-region').func()
        assert runtime._active_jobs==0
        runtime._run_region_startup()
        assert runtime._active_jobs==0
    monkeypatch.setattr(BlockingScheduler,'start',start)
    runtime.run()
    assert calls==['scheduled','startup']


def test_runtime_readonly_creates_no_client_or_write_engine(new_pg_factory, monkeypatch):
    assert importlib.util.find_spec('app.market.region.runtime') is not None, 'region runtime missing'
    runtime=importlib.import_module('app.market.region.runtime')
    sync,factory,client,requests=setup(new_pg_factory)
    @contextmanager
    def readonly(_): yield factory
    def forbidden(*a,**kw): raise AssertionError('must remain readonly')
    monkeypatch.setattr(runtime,'open_readonly_factory',readonly)
    monkeypatch.setattr(runtime,'create_engine_and_session',forbidden)
    monkeypatch.setattr(runtime,'TushareClient',forbidden)
    monkeypatch.setattr(runtime.UniverseResolver,'resolve',lambda *a:POOL)
    with client:
        result=runtime.run_region_update(Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test'),now=NOW,dry_run=True,publish=True)
    assert result['status']=='planned' and result['pendingCount']==2 and requests==[]
