from datetime import datetime,timezone
from types import SimpleNamespace
from app.config import Settings
import app.jobs.routine_update as routine

def test_only_valuation_group_is_paced_counted_and_never_starts_scheduler(monkeypatch):
    monkeypatch.setattr(routine,'load_extension_config',lambda:SimpleNamespace(groups={}))
    monkeypatch.setattr(routine,'portfolio_routine_config',lambda:{'enabled':False})
    monkeypatch.setattr(routine,'financial_supply_config',lambda:{'enabled':False})
    monkeypatch.setattr(routine,'valuation_supply_config',lambda:{'enabled':True},raising=False)
    calls=[]
    def step(settings,**kwargs):
        calls.append(kwargs)
        return {'status':'completed','requestCount':7,'publishReady':True,'publication':{'status':'no_change'}}
    monkeypatch.setattr(routine,'run_valuation_step',step,raising=False)
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test',routine_stocks_enabled=False,routine_macro_enabled=False)
    result=routine.run_routine_update(settings,macro_rebuild=lambda *_:None,now=datetime(2026,11,6,tzinfo=timezone.utc),publish=True)
    assert len(calls)==1 and calls[0]['publish_requested'] is True
    assert result['valuation']['requestCount']==7
    assert result['httpCounts']['total']==7 and result['schedulerStarted'] is False
