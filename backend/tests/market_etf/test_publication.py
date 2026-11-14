import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import PublishedSnapshot, PageSnapshot, SourcePayload
from app.marts.contracts import MartCandidate
from app.market.marts.market import MarketMartBuilder
from app.marts.publication import SnapshotPublisher, QualityCheckFailed
from app.marts.dto import validate_product_payload
from app.stocks.derived.inputs import CoreSnapshot
from backend.tests.market_etf.test_etf import setup, module, DAY, NOW, POOL
from backend.tests.marts.test_content_runtime import state


def prepare(factory):
    from app.db.models import Security, DailyPrice, DailyBasic
    with factory.begin() as session:
        snapshot=CoreSnapshot(DAY,tuple(session.scalars(select(Security))),tuple(session.scalars(select(DailyPrice))),
            tuple(session.scalars(select(DailyBasic))),(),{},(),(),(),(),(),(),())
        for item in MarketMartBuilder().build(snapshot,'baseline'):
            payload=copy.deepcopy(item.payload)
            payload['foreignProof']={'keep':1}
            payload['missingData']=[*payload['missingData'],{'source':'news','reason':'OLD_PERMISSION_FAULT'}]
            session.add(PublishedSnapshot(product=item.product,version='baseline',as_of=item.as_of,payload=payload,
                stale=True,stale_reason='OLD_NEWS_FAULT'))
        session.add(PublishedSnapshot(product='macro-insights',version='macro-old',as_of=NOW,payload={'keep':1}))


def test_etf_publication_changes_only_owned_subscope_and_zero_http_reentry():
    sync,factory,client,calls,_=setup()
    prepare(factory)
    before=state(factory)
    with client: report=sync.run(NOW,publish=True)
    assert report['status']=='completed' and report['publication']['status']=='completed'
    assert report['requestCount']==18
    after=state(factory)
    for key,value in before['pointers'].items():
        if key not in {'market-overview','market-explorer'}: assert after['pointers'][key]==value
    for product in ('market-overview','market-explorer'):
        old=before['pointers'][product]
        new=after['pointers'][product]
        assert new[3:5]==old[3:5]
        if product=='market-overview': assert new[2]==old[2]
        else:
            payload=copy.deepcopy(new[2])
            assert len(payload['heatmapData'].pop('etf'))==9
            payload['heatmapCategories']=[c for c in payload['heatmapCategories'] if c['id']!='etf']
            assert payload==old[2]
            assert validate_product_payload(product,new[2])==()
    assert after['pointers']['market-overview'][0]==after['pointers']['market-explorer'][0]
    with client: assert sync.run(NOW+timedelta(seconds=1),publish=True)['publication']['status']=='no_change'
    assert len(calls)==18 and state(factory)==after


def test_second_product_write_failure_rolls_back_and_retains_collection(monkeypatch):
    sync,factory,client,_,_=setup()
    prepare(factory)
    before=state(factory)
    original=SnapshotPublisher._write_page_snapshot
    def fail(self,session,candidate,version):
        if candidate.product=='market-explorer': raise RuntimeError('private secret')
        return original(self,session,candidate,version)
    monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',fail)
    with client: report=sync.run(NOW,publish=True)
    assert report['status']=='partial' and report['requestCount']==18 and report['publishReady']
    assert state(factory)==before and 'private' not in str(report)
    assert sync.preflight(NOW)['pendingCount']==0


@pytest.mark.parametrize('damage',['remove','older','null_count','stocks','missing_capture','missing_source','bad_capture'])
def test_terminal_guard_or_dto_prevents_etf_lost_or_stock_impersonation(damage):
    sync,factory,client,_,_=setup()
    prepare(factory)
    with client: assert sync.run(NOW,publish=True)['status']=='completed'
    before=state(factory)
    old=before['pointers']['market-explorer'][2]
    payload=copy.deepcopy(old)
    if damage=='remove': payload['heatmapData'].pop('etf')
    elif damage=='older': payload['heatmapData']['etf'][0]['etf']['tradeDate']='2026-10-15'
    elif damage=='null_count': payload['heatmapData']['etf'][0]['stockCount']=1
    elif damage=='stocks': payload['heatmapData']['etf'][0]['stocks']=[{'symbol':'510300.SH'}]
    elif damage=='missing_capture': payload['heatmapData']['etf'][0]['etf'].pop('capturedAt')
    elif damage=='missing_source': payload['heatmapData']['etf'][0]['etf'].pop('source')
    else: payload['heatmapData']['etf'][0]['etf']['capturedAt']['quote']='not-a-timestamp'
    if damage not in {'remove','older'}:
        assert validate_product_payload('market-explorer',payload)
    else:
        with pytest.raises(QualityCheckFailed):
            SnapshotPublisher(factory).publish_many((MartCandidate('market-explorer',NOW,payload),),'bad',())
        assert state(factory)==before


def test_ordinary_market_builder_reads_etf_and_corruption_cannot_erase_it():
    sync,factory,client,_,mod=setup()
    prepare(factory)
    with client: sync.run(NOW,publish=True)
    from app.market.marts.market_runtime import rebuild_market_marts
    assert rebuild_market_marts(factory,POOL,checked_at=NOW,dry_run=False)['status'] in {'completed','no_change'}
    with factory() as session:
        payload=session.get(PublishedSnapshot,'market-explorer').payload
        assert len(payload['heatmapData']['etf'])==9
    with factory.begin() as session:
        raw=session.scalar(select(SourcePayload).where(SourcePayload.api_name=='fund_share'))
        raw.content_hash='bad'
    before=state(factory)
    with pytest.raises(QualityCheckFailed): rebuild_market_marts(factory,POOL,checked_at=NOW,dry_run=False)
    assert state(factory)==before


def test_next_trading_day_rebuild_carries_valid_old_etf_then_normal_entry_advances():
    from datetime import date, datetime, UTC
    from app.db.models import DailyPrice,DailyBasic
    from app.market.data.catalog import plan_market_targets
    from app.market.data.repository import MarketQuoteRepository
    from app.market.data.contracts import QUOTE_FIELDS
    from app.connectors.tushare.contracts import TushareResponse
    from app.market.marts.market_runtime import rebuild_market_marts
    from backend.tests.market_etf.test_etf import response,FIELDS
    import httpx,json
    from app.connectors.tushare.client import TushareClient
    sync,factory,client,_,_=setup()
    prepare(factory)
    with client: assert sync.run(NOW,publish=True)['status']=='completed'
    next_day=date(2026,10,19)
    next_now=datetime(2026,10,19,15,35,tzinfo=UTC)
    with factory.begin() as session:
        session.get(PublishedSnapshot,'screener').as_of=next_now
        for code in POOL.symbols:
            session.add(DailyPrice(ts_code=code,trade_date=next_day,close=10,pct_chg=0))
            session.add(DailyBasic(ts_code=code,trade_date=next_day,data={}))
    # 每日重建可携带仍有完整血缘的旧ETF，但保留各自日期，不冒充新日。
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.jobs.build_and_publish import DefaultMartBuilders
    ordinary=DefaultMartBuilders(factory).build(CoreSnapshotRepository(factory).load(next_day,POOL.symbols),POOL.symbols,'controlled',workbench_symbols=())
    carried=next(c.payload for c in ordinary if c.product=='market-explorer')['heatmapData']['etf']
    assert len(carried)==9 and {t['etf']['tradeDate'] for t in carried}=={'2026-10-16'}
    assert rebuild_market_marts(factory,POOL,checked_at=next_now,dry_run=False,expected_as_of=next_day)['status']=='completed'
    with factory() as session:
        pointer=session.get(PublishedSnapshot,'market-explorer')
        assert pointer.as_of.date()==next_day
        assert {t['etf']['tradeDate'] for t in pointer.payload['heatmapData']['etf']}=={'2026-10-16'}
    quote_repo=MarketQuoteRepository(factory)
    for target in plan_market_targets(sync.catalog,next_now):
        if target.asset.asset_type=='etfs':
            data=TushareResponse(fields=QUOTE_FIELDS,items=((target.asset.asset_code,'20261019',4.236,4.236,4.236,4.236,4.242,-0.14),))
            assert quote_repo.persist(target,data,captured_at=next_now)['status']=='completed'
    calls=[]
    def transport(request):
        body=json.loads(request.content); calls.append(body)
        data=response(body['api_name'],body['params']['ts_code'],day='20261019')
        return httpx.Response(200,json={'code':0,'data':data.model_dump(mode='json')})
    with TushareClient('controlled',transport=httpx.MockTransport(transport),max_retries=0) as next_client:
        sync.client=next_client
        report=sync.run(next_now,publish=True)
        assert report['status']=='completed' and report['requestCount']==18
        assert sync.run(next_now,publish=True)['publication']['status']=='no_change'
    assert len(calls)==18
    with factory() as session:
        assert {t['etf']['tradeDate'] for t in session.get(PublishedSnapshot,'market-explorer').payload['heatmapData']['etf']}=={'2026-10-19'}
