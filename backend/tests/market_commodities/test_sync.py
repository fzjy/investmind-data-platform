import importlib,json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select,func

from app.connectors.tushare.client import TushareClient
from app.db.models import DailyBasic,DailyPrice,Security,PublishedSnapshot,JobRun,SourcePayload
from app.jobs.locks import ProcessLocalJobLock
from app.marts.publication import SnapshotPublisher,QualityCheckFailed
from app.marts.contracts import MartCandidate
from app.marts.dto import validate_product_payload
from backend.tests.market_commodities.test_sources import setup as sources_setup,response,mod,NOW,DAY,POOL
from backend.tests.market_etf.test_publication import prepare
from backend.tests.marts.test_content_runtime import state


def setup(new_pg_factory, budget=60,handler=None):
    _,catalog,factory=sources_setup(new_pg_factory)
    with factory.begin() as session:
        for code in POOL.symbols:
            session.add(Security(ts_code=code,symbol=code[:6],name=code))
            session.flush()
            session.add(DailyPrice(ts_code=code,trade_date=DAY,close=10,pct_chg=0))
            session.add(DailyBasic(ts_code=code,trade_date=DAY,data={}))
        session.add(PublishedSnapshot(product='screener',version='stock-old',as_of=NOW,payload={}))
    prepare(factory)
    catalog=catalog.model_copy(update={'commodities':catalog.commodities.model_copy(update={'max_requests_per_run':budget})})
    calls=[]
    def transport(request):
        body=json.loads(request.content); calls.append(body)
        if handler: return handler(body)
        req=mod('contracts').request(body['api_name'],body['params'],catalog,DAY)
        return httpx.Response(200,json={'code':0,'data':response(req).model_dump(mode='json')})
    client=TushareClient('controlled',transport=httpx.MockTransport(transport),max_retries=0)
    assert importlib.util.find_spec('app.market.commodities.sync') is not None,'commodity sync missing'
    sync=mod('sync').CommoditySync(factory,client,POOL,catalog,ProcessLocalJobLock())
    return sync,factory,client,calls


def test_one_http_budget_resumes_dynamic_requests_then_pair_and_zero_writes(new_pg_factory):
    sync,factory,client,calls=setup(new_pg_factory, 1)
    before=state(factory)
    assert sync.preflight(NOW)['pendingCount']==24 and calls==[]
    with client:
        for _ in range(44): report=sync.run(NOW,publish=True)
        assert report['status']=='completed' and report['requestCount']==1 and report['publication']['status']=='completed'
        assert len(calls)==44
        after=state(factory)
        old_overview=before['pointers']['market-overview'][2]
        new_overview=after['pointers']['market-overview'][2]
        assert {k:v for k,v in new_overview.items() if k!='missingData'}=={k:v for k,v in old_overview.items() if k!='missingData'}
        assert {'source':'commodities','reason':'尚未采集真实来源'} not in new_overview['missingData']
        assert all(row in new_overview['missingData'] for row in old_overview['missingData'] if row['source']!='commodities')
        explorer=after['pointers']['market-explorer'][2]
        assert explorer['heatmapData']==before['pointers']['market-explorer'][2]['heatmapData']
        assert explorer['foreignProof']=={'keep':1}
        assert validate_product_payload('market-explorer',explorer)==()
        assert after['pointers']['macro-insights']==before['pointers']['macro-insights']
        assert sync.run(NOW+timedelta(seconds=1),publish=True)['publication']['status']=='no_change'
        assert state(factory)==after and len(calls)==44


def test_permission_stops_entire_group_and_lock_busy_never_calls(new_pg_factory):
    sync,_,client,calls=setup(new_pg_factory, handler=lambda _:httpx.Response(403))
    with client:
        result=sync.run(NOW)
        assert result['status']=='failed' and result['requestCount']==1 and len(calls)==1
        assert sync.lock.try_acquire('sync-stock-data:representative-50')
        try: assert sync.run(NOW)['status']=='locked'
        finally: sync.lock.release('sync-stock-data:representative-50')
        assert len(calls)==1


def test_pair_failure_rolls_back_but_does_not_lose_collected_components(new_pg_factory, monkeypatch):
    sync,factory,client,calls=setup(new_pg_factory)
    before=state(factory)
    original=SnapshotPublisher._write_page_snapshot
    def fail(self,session,candidate,version):
        if candidate.product=='market-explorer': raise RuntimeError('private secret')
        return original(self,session,candidate,version)
    monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',fail)
    with client: report=sync.run(NOW,publish=True)
    assert report['status']=='partial' and report['requestCount']==44 and report['pendingCount']==0
    assert state(factory)==before and 'private' not in str(report)
    monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',original)
    with client: assert sync.run(NOW,publish=True)['publication']['status']=='completed'
    assert len(calls)==44


def test_terminal_guard_blocks_ordinary_builder_erasing_commodity(new_pg_factory):
    sync,factory,client,_=setup(new_pg_factory)
    with client: assert sync.run(NOW,publish=True)['status']=='completed'
    before=state(factory)
    payload={**before['pointers']['market-explorer'][2],'commodityData':{}}
    with pytest.raises(QualityCheckFailed):
        SnapshotPublisher(factory).publish_many((MartCandidate('market-explorer',NOW,payload),),'bad',())
    assert state(factory)==before


def test_recheck_and_unlock_failures_preserve_real_http(new_pg_factory, monkeypatch):
    sync,_,client,calls=setup(new_pg_factory)
    original=sync.preflight; count=0
    def check(now):
        nonlocal count
        count+=1
        if count>1: raise RuntimeError('private secret')
        return original(now)
    monkeypatch.setattr(sync,'preflight',check)
    release=sync.lock.release; released=[]
    def unlock(key):
        released.append(key); release(key); raise RuntimeError('private secret')
    sync.lock.release=unlock
    with client: result=sync.run(NOW)
    assert result['status']=='failed' and result['requestCount']>0 and result['requestCount']==len(calls)
    assert len(released)==2 and 'private' not in str(result)


def test_cached_mapping_gap_becomes_pending_again_instead_of_zero_http_dead_end(new_pg_factory):
    from backend.tests.market_commodities.test_sources import complete
    sync,factory,_,_=setup(new_pg_factory)
    complete(factory,sync.catalog)
    repo=mod().CommodityRepository(factory,sync.catalog)
    req=next(r for r in mod('contracts').source_requests(sync.catalog,DAY) if r['api']=='fut_mapping')
    data=response(req)
    from app.connectors.tushare.contracts import TushareResponse
    assert repo.persist(req,TushareResponse(fields=data.fields,items=data.items[1:]),NOW) is None
    result=repo.plan(DAY,NOW)
    assert not result['ready'] and any(r['identity']==req['identity'] for r in result['pendingRequests'])


@pytest.mark.parametrize('damage',['source','unit','bars','ohlc','captured','legacy_series',
    'price','change','pre_settle','zero_pre_settle','nan_price'])
def test_dto_rejects_missing_real_commodity_contract(new_pg_factory, damage):
    from backend.tests.market_commodities.test_sources import complete
    sync,factory,_,_=setup(new_pg_factory)
    complete(factory,sync.catalog)
    with factory() as session:
        payload={**session.get(PublishedSnapshot,'market-explorer').payload,**mod().package(session,sync.catalog,DAY,NOW)}
    gold=payload['commodityData']['precious']['domestic'][0]
    if damage=='source': gold.pop('source')
    elif damage=='unit': gold['amountUnit']='qian-yuan'
    elif damage=='bars': gold['bars']['daily']=[]
    elif damage=='ohlc': gold['bars']['daily'][0]['low']=9999
    elif damage=='captured': gold['capturedAtTo']='no-timezone'
    elif damage=='price': gold['price']='999999'
    elif damage=='change': gold['change']=999999
    elif damage=='pre_settle': gold['bars']['daily'][-1].pop('preSettle')
    elif damage=='zero_pre_settle': gold['bars']['daily'][-1]['preSettle']=0
    elif damage=='nan_price': gold['price']='NaN'
    else: gold.pop('dataKind'); gold['series']={'daily':[1,2,3]}
    assert validate_product_payload('market-explorer',payload)


def test_ordinary_builder_keeps_valid_commodity_and_corruption_blocks_erase(new_pg_factory):
    from app.market.marts.market_runtime import rebuild_market_marts
    from app.market.data.catalog import MarketDataCatalog
    from unittest.mock import patch
    sync,factory,client,_=setup(new_pg_factory)
    with client: assert sync.run(NOW,publish=True)['status']=='completed'
    with patch.object(MarketDataCatalog,'load',return_value=sync.catalog):
        assert rebuild_market_marts(factory,POOL,checked_at=NOW,dry_run=False)['status'] in {'completed','no_change'}
        with factory() as session: assert session.get(PublishedSnapshot,'market-explorer').payload['commodityMetadata']['instrumentCount']==10
        with factory.begin() as session: session.scalar(select(SourcePayload)).content_hash='bad'
        before=state(factory)
        with pytest.raises(QualityCheckFailed): rebuild_market_marts(factory,POOL,checked_at=NOW,dry_run=False)
        assert state(factory)==before


def test_next_stock_day_carries_only_verified_old_commodity_with_own_date(new_pg_factory):
    from datetime import date,datetime,UTC
    from unittest.mock import patch
    from app.market.marts.market_runtime import rebuild_market_marts
    from app.market.data.catalog import MarketDataCatalog
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.jobs.build_and_publish import DefaultMartBuilders
    sync,factory,client,_=setup(new_pg_factory)
    with client: assert sync.run(NOW,publish=True)['status']=='completed'
    next_day=date(2026,10,19); next_now=datetime(2026,10,19,15,35,tzinfo=UTC)
    with factory.begin() as session:
        session.get(PublishedSnapshot,'screener').as_of=next_now
        for code in POOL.symbols:
            session.add(DailyPrice(ts_code=code,trade_date=next_day,close=10,pct_chg=0))
            session.add(DailyBasic(ts_code=code,trade_date=next_day,data={}))
    with patch.object(MarketDataCatalog,'load',return_value=sync.catalog):
        candidates=DefaultMartBuilders(factory).build(CoreSnapshotRepository(factory).load(next_day,POOL.symbols),POOL.symbols,'controlled',workbench_symbols=())
        explorer=next(c.payload for c in candidates if c.product=='market-explorer')
        assert explorer['commodityMetadata']['asOf']=='2026-10-16'
        assert rebuild_market_marts(factory,POOL,checked_at=next_now,dry_run=False,expected_as_of=next_day)['status']=='completed'
    with factory() as session:
        pointer=session.get(PublishedSnapshot,'market-explorer')
        assert pointer.as_of.date()==next_day and pointer.payload['commodityMetadata']['asOf']=='2026-10-16'
