import importlib
from contextlib import contextmanager
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler
from typer.testing import CliRunner
from app.config import Settings
from app.cli.main import app
from app.jobs.scheduler import configure_scheduler
from backend.tests.jobs.test_scheduler import _runtime
from backend.tests.market_commodities.test_sync import setup,NOW,POOL,mod


def test_cli_defaults_to_readonly_and_publish_is_explicit(monkeypatch):
    assert CliRunner().invoke(app,['sync-commodities','--help']).exit_code==0
    cli=importlib.import_module('app.cli.commodities')
    calls=[]
    monkeypatch.setattr(Settings,'from_env',lambda:Settings())
    monkeypatch.setattr(cli,'run_commodity_update',lambda settings,**kw:calls.append((kw['dry_run'],kw['publish'])) or {'status':'planned','requestCount':0})
    for args in ([],['--publish'],['--dry-run','--publish']): assert CliRunner().invoke(app,['sync-commodities',*args]).exit_code==0
    assert calls==[(True,False),(False,True),(True,True)]


def test_unified_commodity_group_independent_total_and_order(monkeypatch):
    from app.jobs import routine_update as routine
    assert 'routine_commodities_enabled' in Settings.model_fields
    calls=[]
    for name in ('etf','commodity'):
        monkeypatch.setattr(routine,'run_'+name+'_step',lambda *a,name=name,**kw:calls.append(name) or {
            'status':'completed','requestCount':1 if name=='etf' else 44,'publishReady':True,'publication':{'status':'no_change'}})
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,routine_macro_enabled=False,
        routine_etf_heatmap_enabled=True,routine_commodities_enabled=True)
    result=routine.run_routine_update(settings,now=NOW,publish=True,macro_rebuild=lambda _:None)
    assert result['status']=='completed' and calls==['etf','commodity']
    assert result['httpCounts']['total']==45 and result['httpCounts']['commodities']==44


def test_readonly_runtime_no_client_write_engine_or_locks(new_pg_factory, monkeypatch):
    assert importlib.util.find_spec('app.market.commodities.runtime') is not None,'commodity runtime missing'
    runtime=mod('runtime'); sync,factory,client,calls=setup(new_pg_factory)
    @contextmanager
    def readonly(_): yield factory
    def forbidden(*a,**kw): raise AssertionError('not readonly')
    monkeypatch.setattr(runtime,'open_readonly_factory',readonly)
    monkeypatch.setattr(runtime,'create_engine_and_session',forbidden)
    monkeypatch.setattr(runtime,'TushareClient',forbidden)
    monkeypatch.setattr(runtime.MarketDataCatalog,'load',lambda:sync.catalog)
    monkeypatch.setattr(runtime.UniverseResolver,'resolve',lambda *a:POOL)
    with client: report=runtime.run_commodity_update(Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test'),now=NOW,dry_run=True,publish=True)
    assert report['status']=='planned' and report['pendingCount']==24 and calls==[]


def test_daily_schedule_lifecycle_guard_and_startup(new_pg_factory, monkeypatch):
    assert 'routine_commodities_enabled' in Settings.model_fields
    sync,_,_,_=setup(new_pg_factory); catalog=sync.catalog
    scheduler=BlockingScheduler(timezone='Asia/Shanghai')
    configure_scheduler(scheduler,Settings(routine_commodities_enabled=True),SimpleNamespace(run_scheduled=lambda _:None,run_startup=lambda _:None),
        clock=lambda:NOW,commodity_job=lambda:None,commodity_schedule=catalog.schedule)
    job=scheduler.get_job('sync-commodities')
    assert job.max_instances==1 and job.coalesce and job.trigger.get_next_fire_time(None,NOW).isoformat()=='2026-10-17T11:00:00+00:00'
    runtime=_runtime([])
    runtime._settings=runtime._settings.model_copy(update={'routine_commodities_enabled':True})
    calls=[]
    runtime._commodity_coordinator=SimpleNamespace(catalog=catalog,
        run_scheduled=lambda now:calls.append('scheduled') or {'status':'completed'},
        run_startup=lambda now:calls.append('startup') or {'status':'completed'})
    def start(scheduler):
        scheduler.get_job('sync-commodities').func(); runtime._run_commodity_startup()
        assert runtime._active_jobs==0
    monkeypatch.setattr(BlockingScheduler,'start',start)
    runtime.run()
    assert calls==['scheduled','startup']
