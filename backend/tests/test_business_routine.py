from datetime import datetime,timezone
from types import SimpleNamespace
from app.config import Settings
import app.jobs.routine_update as routine


def switches(monkeypatch):
    monkeypatch.setattr(routine,'load_extension_config',lambda:SimpleNamespace(groups={}))
    monkeypatch.setattr(routine,'portfolio_routine_config',lambda:{'enabled':False})
    monkeypatch.setattr(routine,'financial_supply_config',lambda:{'enabled':False})
    monkeypatch.setattr(routine,'valuation_supply_config',lambda:{'enabled':False})
    monkeypatch.setattr(routine,'business_supply_config',lambda:{'enabled':True},raising=False)


def test_business_group_is_paced_counted_published_and_never_starts_scheduler(monkeypatch):
    switches(monkeypatch);calls=[]
    def step(settings,**kwargs):
        calls.append(kwargs)
        return {'status':'completed','requestCount':7,'publishReady':True,'publication':{'status':'no_change'}}
    monkeypatch.setattr(routine,'run_business_step',step,raising=False)
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,routine_macro_enabled=False)
    report=routine.run_routine_update(settings,macro_rebuild=lambda *_:None,now=datetime(2026,11,6,tzinfo=timezone.utc),publish=True)
    assert report['status']=='completed' and report['business']['requestCount']==7
    assert report['httpCounts']['total']==7 and report['schedulerStarted'] is False
    assert len(calls)==1 and calls[0]['publish_requested'] is True


def test_only_portfolio_does_not_select_business_even_when_config_enabled(monkeypatch):
    switches(monkeypatch)
    monkeypatch.setattr(routine,'portfolio_routine_config',lambda:{'enabled':True})
    monkeypatch.setattr(routine,'run_portfolio_step',lambda *a,**kw:dict(status='completed',requestCount=0,publishReady=True,publication={'status':'no_change'}))
    def forbidden(*a,**kw):raise AssertionError('only-portfolio不能调用主营组')
    monkeypatch.setattr(routine,'run_business_step',forbidden,raising=False)
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,routine_macro_enabled=False)
    report=routine.run_routine_update(settings,macro_rebuild=lambda *_:None,only_portfolio=True,publish=True)
    assert report['status']=='completed' and 'business' not in report
    assert report['httpCounts']['total']==0
