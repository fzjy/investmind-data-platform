import importlib
from contextlib import contextmanager
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler
from typer.testing import CliRunner

from app.config import Settings
from app.cli.main import app
from app.market.data.catalog import MarketDataCatalog
from app.jobs.scheduler import configure_scheduler
from backend.tests.market_etf.test_etf import setup, module, NOW, POOL
from backend.tests.jobs.test_scheduler import _runtime


def test_cli_default_readonly_and_explicit_publish(monkeypatch):
    result=CliRunner().invoke(app,['sync-etf-heatmap','--help'])
    assert result.exit_code==0, result.output
    mod=importlib.import_module('app.cli.etf_heatmap')
    calls=[]
    monkeypatch.setattr(Settings,'from_env',lambda:Settings())
    def run(settings,*,now,dry_run,publish):
        calls.append((dry_run,publish))
        return {'status':'planned' if dry_run else 'completed','requestCount':0}
    monkeypatch.setattr(mod,'run_etf_update',run)
    for args in ([],['--publish'],['--publish','--dry-run']):
        assert CliRunner().invoke(app,['sync-etf-heatmap',*args]).exit_code==0
    assert calls==[(True,False),(False,True),(True,True)]


def test_etf_group_is_independent_and_counted_in_unified_entry(monkeypatch):
    from app.jobs import routine_update as mod
    assert 'routine_etf_heatmap_enabled' in Settings.model_fields
    calls=[]
    monkeypatch.setattr(mod,'run_etf_step',lambda *a,**kw:calls.append('etf') or {
        'status':'completed','requestCount':18,'publishReady':True,'publication':{'status':'no_change'}})
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,
        routine_macro_enabled=False,routine_etf_heatmap_enabled=True)
    result=mod.run_routine_update(settings,now=NOW,publish=True,macro_rebuild=lambda _:None)
    assert calls==['etf'] and result['status']=='completed'
    assert result['httpCounts']=={'stocks':0,'macro':0,'etf':18,'total':18}
    def fail(*a,**kw): raise RuntimeError('private secret')
    monkeypatch.setattr(mod,'run_etf_step',fail)
    monkeypatch.setattr(mod,'run_macro_catchup',lambda *a,**kw:calls.append('macro') or {'status':'completed','activeCoverageCompleted':True,'requestCount':0})
    report=mod.run_routine_update(settings.model_copy(update={'routine_macro_enabled':True}),now=NOW,macro_rebuild=lambda _:None)
    assert calls==['etf','macro'] and report['status']=='failed' and report['httpCounts']['total'] is None
    assert 'private' not in str(report)


def test_scheduler_etf_daily_trigger_and_guard(monkeypatch):
    assert 'routine_etf_heatmap_enabled' in Settings.model_fields
    scheduler=BlockingScheduler(timezone='Asia/Shanghai')
    catalog=MarketDataCatalog.load()
    catalog=catalog.model_copy(update={'etf_heatmap':catalog.etf_heatmap.model_copy(update={'enabled':True})})
    configure_scheduler(scheduler,Settings(routine_etf_heatmap_enabled=True),
        SimpleNamespace(run_scheduled=lambda _:None,run_startup=lambda _:None),clock=lambda:NOW,
        etf_job=lambda:None,etf_schedule=catalog.schedule)
    job=scheduler.get_job('sync-etf-heatmap')
    assert job.max_instances==1 and job.coalesce
    assert job.trigger.get_next_fire_time(None,NOW).isoformat()=='2026-10-17T11:00:00+00:00'
    runtime=_runtime([])
    assert hasattr(runtime,'_etf_coordinator')
    runtime._settings=runtime._settings.model_copy(update={'routine_etf_heatmap_enabled':True})
    calls=[]
    runtime._etf_coordinator=SimpleNamespace(catalog=catalog,
        run_scheduled=lambda now:calls.append('scheduled') or {'status':'completed'},
        run_startup=lambda now:calls.append('startup') or {'status':'completed'})
    def start(scheduler):
        scheduler.get_job('sync-etf-heatmap').func()
        runtime._run_etf_startup()
        assert runtime._active_jobs==0
    monkeypatch.setattr(BlockingScheduler,'start',start)
    runtime.run()
    assert calls==['scheduled','startup']


def test_runtime_readonly_has_no_client_engine_or_locks(new_pg_factory, monkeypatch):
    assert importlib.util.find_spec('app.market.etf.runtime') is not None, 'ETF runtime missing'
    mod=module('runtime')
    sync,factory,client,calls,_=setup(new_pg_factory)
    @contextmanager
    def readonly(_): yield factory
    def forbidden(*a,**kw): raise AssertionError('not readonly')
    monkeypatch.setattr(mod,'open_readonly_factory',readonly)
    monkeypatch.setattr(mod,'create_engine_and_session',forbidden)
    monkeypatch.setattr(mod,'TushareClient',forbidden)
    monkeypatch.setattr(mod.MarketDataCatalog,'load',lambda:sync.catalog)
    monkeypatch.setattr(mod.UniverseResolver,'resolve',lambda *a:POOL)
    with client:
        report=mod.run_etf_update(Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test'),now=NOW,dry_run=True,publish=True)
    assert report['status']=='planned' and report['pendingCount']==18 and calls==[]
