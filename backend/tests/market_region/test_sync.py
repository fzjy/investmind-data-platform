import importlib
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, func

from app.connectors.tushare.client import TushareClient
from app.db.models import DailyPrice, DailyBasic, Security, PublishedSnapshot, JobRun, SourcePayload
from app.jobs.locks import ProcessLocalJobLock
from app.market.data.catalog import MarketDataCatalog
from backend.tests.market_pulse.test_sync import POOL, DAY, NOW, CODE, OTHER, _factory


def setup(budget=1, handler=None):
    assert importlib.util.find_spec('app.market.region') is not None, 'region sync missing'
    factory = _factory()
    with factory.begin() as session:
        for code in POOL.symbols:
            session.add(Security(ts_code=code, symbol=code[:6], name=code, data={'keep':1}))
            session.add(DailyPrice(ts_code=code, trade_date=DAY, close=10, pct_chg=0))
            session.add(DailyBasic(ts_code=code, trade_date=DAY, data={}))
        session.add(PublishedSnapshot(product='screener', version='old', as_of=NOW, payload={}))
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        return handler(body) if handler else httpx.Response(200, json={'code':0, 'data': {
            'fields':['ts_code','area'], 'items':[[body['params']['ts_code'], '贵州' if body['params']['ts_code']==CODE else '深圳']]}})
    client = TushareClient('controlled', transport=httpx.MockTransport(transport), max_retries=0)
    catalog = MarketDataCatalog.load()
    catalog = catalog.model_copy(update={'region':catalog.region.model_copy(update={'max_requests_per_run':budget})})
    sync = importlib.import_module('app.market.region.sync').RegionSync(factory, client, POOL, catalog, ProcessLocalJobLock())
    return sync, factory, client, requests


def test_budget_resume_cache_expiry_and_zero_http():
    sync, factory, client, requests = setup()
    with client:
        assert sync.preflight(NOW)['pendingCount'] == 2 and requests == []
        first = sync.run(NOW)
        assert first['status']=='partial' and first['requestCount']==1
        second = sync.run(NOW+timedelta(seconds=1))
        assert second['status']=='completed' and second['requestCount']==1
        assert requests[1]['params']=={'ts_code':OTHER} and requests[1]['fields']=='ts_code,area'
        assert sync.run(NOW+timedelta(days=1))['requestCount']==0
        assert sync.preflight(NOW+timedelta(days=31))['pendingCount']==2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePayload))==2


def test_metadata_budget_change_does_not_invalidate_cursor():
    sync, _, client, requests = setup()
    with client:
        sync.run(NOW)
        fingerprint = sync.fingerprint
        changed = sync.catalog.model_copy(update={'region':sync.catalog.region.model_copy(update={'max_requests_per_run':60,'timeout_seconds':30})})
        other = type(sync)(sync.factory, client, POOL, changed, sync.lock)
        assert other.fingerprint == fingerprint
        assert other.preflight(NOW)['pendingSymbols']==[OTHER]
        assert other.run(NOW)['requestCount']==1 and len(requests)==2


def test_permission_stops_entire_source():
    sync, factory, client, requests = setup(60, lambda _:httpx.Response(403))
    with client: result=sync.run(NOW)
    assert result['status']=='failed' and result['requestCount']==1 and len(requests)==1
    assert not result['publishReady'] and result['pendingCount']==2
    with factory() as session:
        assert all('area' not in row.data for row in session.scalars(select(Security)))


def test_stock_lock_and_preflight_never_write_or_call():
    sync, factory, client, requests = setup()
    assert sync.lock.try_acquire('sync-stock-data:representative-50')
    try:
        with client:
            sync.preflight(NOW)
            assert sync.run(NOW)['status']=='locked'
    finally: sync.lock.release('sync-stock-data:representative-50')
    assert requests==[]
    with factory() as session: assert session.scalar(select(func.count()).select_from(JobRun))==0


def test_capture_uses_response_clock_not_start_of_whole_job():
    from app.market.data.repository import aware
    sync,factory,client,_=setup(60)
    captured=NOW+timedelta(minutes=2)
    sync.clock=lambda:captured
    with client: report=sync.run(NOW)
    assert report['requestCount']==2
    with factory() as session:
        assert {aware(raw.captured_at) for raw in session.scalars(select(SourcePayload))}=={captured}


def test_conflicting_written_region_never_counts_as_complete(monkeypatch):
    sync,factory,client,_=setup(60)
    with factory.begin() as session: session.get(Security,CODE).data={'region':'广东'}
    def forbidden(*a,**kw): raise AssertionError('invalid region must not publish')
    monkeypatch.setattr(importlib.import_module('app.market.region.sync'),'rebuild_market_marts',forbidden)
    with client: report=sync.run(NOW,publish=True)
    assert report['requestCount']==2 and report['status']=='partial' and not report['publishReady']
    assert report['pendingSymbols']==[CODE] and 'publication' not in report


def test_previously_complete_cache_expiring_during_run_is_not_publish_ready(monkeypatch):
    from app.market.region.repository import RegionRepository
    from backend.tests.market_region.test_repository import response
    sync,factory,client,_=setup(60)
    repo=RegionRepository(factory)
    assert repo.persist(CODE,sync.fingerprint,response(),NOW-timedelta(days=30)+timedelta(seconds=1))
    sync.clock=lambda:NOW+timedelta(seconds=2)
    def forbidden(*a,**kw): raise AssertionError('expired region must not publish')
    monkeypatch.setattr(importlib.import_module('app.market.region.sync'),'rebuild_market_marts',forbidden)
    with client: report=sync.run(NOW,publish=True)
    assert report['requestCount']==1 and report['pendingSymbols']==[CODE] and not report['publishReady']
    assert 'publication' not in report


def test_unlock_error_preserves_http_and_attempts_all_releases():
    sync,_,client,requests=setup(60)
    original=sync.lock.release
    releases=[]
    def release(key):
        releases.append(key)
        original(key)
        raise RuntimeError('secret unlock failure')
    sync.lock.release=release
    with client: report=sync.run(NOW)
    assert report['requestCount']==2 and len(requests)==2 and report['pendingSymbols']==[]
    assert releases==['sync-stock-data:representative-50','sync-security-region']
    assert report['status']=='failed' and report['cleanupError']=='REGION_UNLOCK_FAILED'
    assert 'secret' not in str(report)


@pytest.mark.parametrize('failure',['target_recheck','publisher'])
def test_publish_error_retains_actual_http_and_checkpoint(monkeypatch, failure):
    sync, _, client, requests = setup(60)
    mod=importlib.import_module('app.market.region.sync')
    from app.market.region.repository import RegionRepository
    def fail(*args,**kwargs): raise RuntimeError('secret credentials')
    if failure=='publisher': monkeypatch.setattr(mod,'rebuild_market_marts',fail)
    else:
        original, calls = RegionRepository.target, 0
        def target(self,*args):
            nonlocal calls
            calls+=1
            return original(self,*args) if calls==1 else fail()
        monkeypatch.setattr(RegionRepository,'target',target)
    with client: report=sync.run(NOW,publish=True)
    assert report['status']=='partial' and report['requestCount']==2 and len(requests)==2
    assert report['pendingSymbols']==[] and report['publication']['code']=='REGION_PUBLICATION_FAILED'
    assert 'secret' not in str(report)
