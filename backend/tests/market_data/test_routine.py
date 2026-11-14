import importlib
from datetime import timedelta

from app.config import Settings
from backend.tests.market_data.test_repository import NOW, setup
from backend.tests.market_data.test_catalog import catalog
from backend.tests.jobs.test_routine_update import setup_steps, settings


def test_legacy_two_group_update_does_not_select_market(monkeypatch):
    assert 'routine_market_enabled' in Settings.model_fields, 'market selection missing'
    assert Settings().routine_market_enabled is False
    module, calls = setup_steps(monkeypatch)
    report = module.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert report['market']['status'] == 'not_selected'
    assert report['httpCounts'] == {'stocks': 2, 'macro': 3, 'total': 5}


def test_market_failure_does_not_block_macro(monkeypatch):
    module, calls = setup_steps(monkeypatch)
    assert hasattr(module, 'run_market_step'), 'market routine step missing'
    def fail(*args, **kwargs):
        raise RuntimeError('secret-token')
    monkeypatch.setattr(module, 'run_market_step', fail)
    report = module.run_routine_update(settings(routine_market_enabled=True), now=NOW, macro_rebuild=lambda s: None)
    assert calls[-1][0] == 'macro'
    assert report['status'] == 'failed' and report['market']['requestCount'] is None
    assert report['httpCounts']['total'] is None and 'secret-token' not in str(report)


def test_market_only_group_and_publish_ready(monkeypatch):
    module, _ = setup_steps(monkeypatch)
    assert hasattr(module, 'run_market_step'), 'market routine step missing'
    monkeypatch.setattr(module, 'run_market_step', lambda *a, **k: {'status': 'not_due', 'requestCount': 0, 'publishReady': True})
    report = module.run_routine_update(settings(routine_stocks_enabled=False, routine_macro_enabled=False, routine_market_enabled=True),
        now=NOW, publish=True, macro_rebuild=lambda s: None)
    assert report['status'] == 'completed' and report['httpCounts']['total'] == 0


def test_restart_after_week_only_plans_latest_slot_and_bad_core_recovers():
    assert importlib.util.find_spec('app.market.data.schedule_repository') is not None, 'schedule repository missing'
    from app.market.data.schedule_repository import MarketScheduleRepository
    from app.market.data.catalog import plan_market_targets
    from backend.tests.market_data.test_sync import job
    value, factory, _ = setup()
    repo = MarketScheduleRepository(factory)
    info = repo.inspect(catalog(), now=NOW)
    assert info['latestDueAt'] == '2026-10-15T11:00:00+00:00' and info['collected'] is False
    sync, client, _ = job(factory)
    try:
        report = sync.run(NOW).to_payload()
    finally:
        client.close()
    from datetime import datetime
    repo.record(catalog(), slot=datetime.fromisoformat(info['latestDueAt']), targets=plan_market_targets(catalog(), NOW),
        checked_at=NOW, collection=report, publication=None)
    assert repo.inspect(catalog(), now=NOW)['collected'] is True
    assert repo.inspect(catalog(), now=NOW)['published'] is False
    assert repo.inspect(catalog(), now=NOW + timedelta(days=7))['latestDueAt'] == '2026-10-22T11:00:00+00:00'
    assert repo.inspect(catalog(), now=NOW + timedelta(days=7))['collected'] is False
    with factory.begin() as session:
        session.query(value.MarketDailyQuote).filter_by(asset_code='000001.SH').delete()
    assert repo.inspect(catalog(), now=NOW)['collected'] is False


def test_collected_unpublished_cycle_can_publish_with_zero_http(monkeypatch):
    from app.market.data import runtime as module
    from app.market.data.catalog import plan_market_targets
    from app.market.data.schedule_repository import MarketScheduleRepository
    from app.market.data.scheduling import most_recent_market_schedule
    from backend.tests.market_data.test_cli import runtime
    from backend.tests.marts.test_content_runtime import state
    mod, factory = runtime(monkeypatch, seeded=True)
    cat = catalog()
    MarketScheduleRepository(factory).record(cat, slot=most_recent_market_schedule(NOW,cat.schedule),
        targets=plan_market_targets(cat,NOW), checked_at=NOW, collection={'status':'completed'}, publication=None)
    def forbidden(*a, **kw):
        raise AssertionError('must reuse collected targets')
    monkeypatch.setattr(mod, 'TushareClient', forbidden)
    tomorrow_morning = NOW + timedelta(hours=15)
    report = module.run_market_step(Settings(data_backend='database',database_url='sqlite:///controlled'),
        now=tomorrow_morning, dry_run=False, publish=True)
    assert report['status'] == 'completed' and report['requestCount'] == 0
    before = state(factory)
    assert module.run_market_step(Settings(data_backend='database',database_url='sqlite:///controlled'),
        now=tomorrow_morning, dry_run=False, publish=True)['status'] == 'not_due'
    assert state(factory) == before


def test_routine_applies_real_job_pacer_between_groups(monkeypatch):
    from app.jobs.supplier_job_pacing import SupplierJobPacer
    module, _ = setup_steps(monkeypatch)
    assert hasattr(module, 'SupplierJobPacer'), 'routine boundary pacing missing'
    clock, times = [0.0], []
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(module, 'SupplierJobPacer', lambda rpm: SupplierJobPacer(60,clock=lambda:clock[0],sleep=sleep))
    def stock(*a, **kw):
        times.append(clock[0])
        return {'status':'completed','publishReady':True,'requests':1}
    def market(*a, **kw):
        times.append(clock[0])
        return {'status':'completed','publishReady':True,'requestCount':1}
    def macro(*a, **kw):
        times.append(clock[0])
        return {'status':'completed','activeCoverageCompleted':True,'requestCount':1}
    monkeypatch.setattr(module,'run_stock_step',stock)
    monkeypatch.setattr(module,'run_market_step',market)
    monkeypatch.setattr(module,'run_macro_catchup',macro)
    report = module.run_routine_update(settings(routine_market_enabled=True), now=NOW,macro_rebuild=lambda s:None)
    assert report['httpCounts']['total'] == 3 and times == [0.0,1.0,2.0]


import pytest


@pytest.mark.parametrize('lookback',[1,14])
def test_partial_cycle_resumes_frozen_asset_targets_across_us_close(monkeypatch, lookback):
    import httpx, json
    from app.connectors.tushare.client import TushareClient
    from app.market.data import runtime as module
    from backend.tests.market_data.test_cli import runtime
    mod, factory = runtime(monkeypatch)
    cat_payload = catalog().model_dump(mode='json',by_alias=True)
    cat_payload['sync']['maxRequestsPerRun'] = 4
    cat_payload['sync']['lookbackDays'] = lookback
    cat = type(catalog()).model_validate(cat_payload)
    monkeypatch.setattr(mod.MarketDataCatalog, 'load', lambda:cat)
    calls = []
    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200,json={'code':0,'data':{
            'fields':['ts_code','trade_date','open','high','low','close','pre_close','pct_chg'],
            'items':[[payload['params']['ts_code'],payload['params']['end_date'],100,100,100,100,100,0]]}})
    monkeypatch.setattr(mod, 'TushareClient', lambda token, **kwargs:TushareClient(token,transport=httpx.MockTransport(transport),max_retries=0))
    configured = Settings(data_backend='database',database_url='sqlite:///controlled',tushare_token='controlled')
    assert module.run_market_step(configured,now=NOW,dry_run=False,publish=False)['status'] == 'partial'
    second = module.run_market_step(configured,now=NOW+timedelta(hours=15),dry_run=False,publish=False)
    assert second['requestCount'] == 4
    assert [call['params']['ts_code'] for call in calls[4:]] == ['SPX','IXIC','510050.SH','510300.SH']
    assert calls[4]['params']['end_date'] == '20261014'


def test_tampered_completed_cycle_cannot_claim_zero_http_ready():
    import copy
    from datetime import date, datetime
    from sqlalchemy import select
    from app.db.models import JobCursor
    from app.market.data.catalog import plan_market_targets
    from app.market.data.schedule_repository import MarketScheduleRepository
    from app.market.data.scheduling import most_recent_market_schedule
    from backend.tests.market_data.test_sync import job
    _, factory, _ = setup()
    sync, client, _ = job(factory)
    try:
        sync.run(NOW)
    finally:
        client.close()
    cat = catalog()
    repository = MarketScheduleRepository(factory)
    repository.record(cat,slot=most_recent_market_schedule(NOW,cat.schedule),targets=plan_market_targets(cat,NOW),
        checked_at=NOW,collection={'status':'completed'},publication=None)
    with factory.begin() as session:
        record = session.scalar(select(JobCursor).where(JobCursor.job_name=='market-schedule'))
        proof = copy.deepcopy(record.cursor_value)
        target = proof['targets'][0]
        for key in ('startDate','endDate'):
            target[key] = (date.fromisoformat(target[key])+timedelta(days=1)).isoformat()
        record.cursor_value = proof
    assert repository.inspect(cat,now=NOW+timedelta(days=0.5))['collected'] is False
