import importlib
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, func

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.contracts import TushareResponse
from app.db.models import DailyBasic, DailyPrice, PublishedSnapshot, Security, SourcePayload, JobCursor, JobRun, MarketDailyQuote
from app.jobs.locks import ProcessLocalJobLock
from app.market.data.catalog import MarketDataCatalog, plan_market_targets
from app.market.data.contracts import QUOTE_FIELDS
from app.market.data.repository import MarketQuoteRepository
from backend.tests.market_pulse.test_sync import POOL, DAY, NOW

CODE = '510300.SH'
FIELDS = {'fund_daily': ('ts_code','trade_date','close','pct_chg','vol','amount'),
          'fund_share': ('ts_code','trade_date','fd_share')}


def module(name='repository'):
    assert importlib.util.find_spec('app.market.etf') is not None, 'ETF source group missing'
    return importlib.import_module('app.market.etf.' + name)


def response(api, code=CODE, day='20261016', value=None):
    values = [code, day, 4.236, -0.14, 37995084.74, 16092791.436] if api == 'fund_daily' else [code, day, 2855298.77]
    if value is not None:
        values[-1] = value
    return TushareResponse(fields=FIELDS[api], items=(tuple(values),))


def setup(new_pg_factory, budget=20, handler=None):
    mod = module()
    factory = new_pg_factory()
    catalog = MarketDataCatalog.load()
    catalog = catalog.model_copy(update={'etf_heatmap': catalog.etf_heatmap.model_copy(update={'enabled':True, 'max_requests_per_run':budget})})
    with factory.begin() as session:
        for code in POOL.symbols:
            session.add(Security(ts_code=code, symbol=code[:6], name=code))
            session.flush()
            session.add(DailyPrice(ts_code=code, trade_date=DAY, close=10, pct_chg=0))
            session.add(DailyBasic(ts_code=code, trade_date=DAY, data={}))
        session.add(PublishedSnapshot(product='screener', version='old', as_of=NOW, payload={}))
    quote_repo = MarketQuoteRepository(factory)
    for target in plan_market_targets(catalog, NOW):
        if target.asset.asset_type == 'etfs':
            quote = TushareResponse(fields=QUOTE_FIELDS, items=((target.asset.asset_code,'20261016',4.236,4.236,4.236,4.236,4.242,-0.14),))
            assert quote_repo.persist(target, quote, captured_at=NOW)['status'] == 'completed'
    calls = []
    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        if handler:
            return handler(body)
        data = response(body['api_name'], body['params']['ts_code'])
        return httpx.Response(200, json={'code':0, 'data':data.model_dump(mode='json')})
    client = TushareClient('controlled', transport=httpx.MockTransport(transport), max_retries=0)
    sync = module('sync').EtfSync(factory, client, POOL, catalog, ProcessLocalJobLock())
    return sync, factory, client, calls, mod


def test_two_components_resume_and_exact_units_and_same_target_zero_writes(new_pg_factory):
    sync, factory, client, calls, mod = setup(new_pg_factory, 1)
    with client:
        assert sync.preflight(NOW)['pendingCount'] == 18 and calls == []
        for count in range(18):
            report = sync.run(NOW)
            assert report['requestCount'] == 1
            assert report['pendingCount'] == 17-count
        assert report['status'] == 'completed' and report['publishReady']
        assert calls[1]['api_name'] == 'fund_share'
        assert calls[1]['params'] == {'ts_code':'510050.SH','trade_date':'20261016'}
        assert calls[1]['fields'] == 'ts_code,trade_date,fd_share'
        with factory() as session:
            tiles = mod.tiles(session, sync.catalog, DAY, NOW)
            tile = next(row for row in tiles if row['id'] == 'etf:'+CODE)
            assert tile['marketCap'] == pytest.approx(1209.504558972)
            assert tile['turnover'] == pytest.approx(160.92791436)
            assert tile['stockCount'] is None and tile['stocks'] == []
            assert tile['etf']['shares'] == 2855298.77
            before = session.scalar(select(func.count()).select_from(SourcePayload))
            runs = session.scalar(select(func.count()).select_from(JobRun))
        assert sync.run(NOW+timedelta(seconds=1))['requestCount'] == 0 and len(calls) == 18
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(SourcePayload)) == before
            assert session.scalar(select(func.count()).select_from(JobRun)) == runs


@pytest.mark.parametrize('api,bad', [
    ('fund_share', response('fund_share', day='20261015')),
    ('fund_share', response('fund_share', code='000001.SH')),
    *[('fund_share',response('fund_share',value=v)) for v in (-1,0,True,'NaN','Infinity')],
    ('fund_daily',response('fund_daily',value=-1)),
    ('fund_share',TushareResponse(fields=FIELDS['fund_share'],items=((CODE,'20261016',1),)*2)),
])
def test_invalid_component_never_gets_success_checkpoint(new_pg_factory, api,bad):
    sync, factory, _, _, mod = setup(new_pg_factory)
    repo = mod.EtfRepository(factory)
    assert repo.persist(CODE,DAY,api,sync.fingerprint,bad,NOW) == 'ETF_RESPONSE_REJECTED'
    assert repo.component(CODE,DAY,api,sync.fingerprint,NOW) is None


def test_legal_missing_is_distinct_and_zero_turnover_is_real(new_pg_factory):
    sync, factory, _, _, mod = setup(new_pg_factory)
    repo=mod.EtfRepository(factory)
    assert repo.persist(CODE,DAY,'fund_share',sync.fingerprint,TushareResponse(fields=FIELDS['fund_share'],items=()),NOW) == 'ETF_SOURCE_NOT_AVAILABLE'
    assert repo.persist(CODE,DAY,'fund_daily',sync.fingerprint,response('fund_daily',value=0),NOW) is None
    assert repo.component(CODE,DAY,'fund_daily',sync.fingerprint,NOW)['amount'] == Decimal(0)


@pytest.mark.parametrize('damage',['hash','cursor_bool','batch','future','past_capture','cursor_capture','batch_capture','core','core_raw','request'])
def test_corrupt_lineage_or_quote_cannot_be_published(new_pg_factory, damage):
    sync,factory,client,_,mod=setup(new_pg_factory)
    with client: assert sync.run(NOW)['status']=='completed'
    with factory.begin() as session:
        cursor=session.scalar(select(JobCursor).where(JobCursor.job_name==mod.JOB_NAME))
        raw=session.get(SourcePayload,cursor.cursor_value['sourcePayloadId'])
        if damage=='hash': raw.content_hash='x'*64
        elif damage=='cursor_bool': cursor.cursor_value={**cursor.cursor_value,'sourcePayloadId':True}
        elif damage=='batch':
            from app.db.models import IngestionBatch
            session.get(IngestionBatch,raw.batch_id).status='failed'
        elif damage=='future': raw.captured_at=NOW+timedelta(days=1)
        elif damage=='past_capture': raw.captured_at=NOW-timedelta(days=30)
        elif damage=='cursor_capture': cursor.updated_at=NOW-timedelta(seconds=1)
        elif damage=='batch_capture':
            from app.db.models import IngestionBatch
            session.get(IngestionBatch,raw.batch_id).finished_at=NOW-timedelta(seconds=1)
        elif damage=='request': raw.request_hash='x'*64
        elif damage=='core': session.scalar(select(MarketDailyQuote)).pct_chg=Decimal('2')
        else:
            quote=session.scalar(select(MarketDailyQuote))
            session.get(SourcePayload,quote.source_payload_id).content_hash='x'*64
    with factory() as session: assert mod.tiles(session,sync.catalog,DAY,NOW)==[]


def test_budget_option_change_keeps_checkpoint_and_quote_identity(new_pg_factory):
    sync,_,client,calls,_=setup(new_pg_factory, 1)
    with client: sync.run(NOW)
    changed=sync.catalog.model_copy(update={'etf_heatmap':sync.catalog.etf_heatmap.model_copy(update={'max_requests_per_run':20,'timeout_seconds':30})})
    other=type(sync)(sync.factory,client,POOL,changed,sync.lock)
    assert other.fingerprint==sync.fingerprint and changed.fingerprint==sync.catalog.fingerprint
    assert other.preflight(NOW)['pendingCount']==17 and len(calls)==1


def test_permission_stops_once_and_locks_block_without_http(new_pg_factory):
    sync,_,client,calls,_=setup(new_pg_factory, handler=lambda _:httpx.Response(403))
    with client:
        report=sync.run(NOW)
        assert report['status']=='failed' and report['requestCount']==1 and len(calls)==1
        assert sync.lock.try_acquire('sync-market-data')
        try: assert sync.run(NOW)['status']=='locked'
        finally: sync.lock.release('sync-market-data')
        assert len(calls)==1


def test_recheck_error_keeps_real_http_and_completed_checkpoints(new_pg_factory, monkeypatch):
    sync,_,client,calls,_=setup(new_pg_factory)
    original=sync.preflight
    count=0
    def preflight(now):
        nonlocal count
        count+=1
        if count>1: raise RuntimeError('private secret')
        return original(now)
    monkeypatch.setattr(sync,'preflight',preflight)
    with client: report=sync.run(NOW,publish=True)
    assert report['requestCount']==18 and len(calls)==18 and report['status']=='failed'
    assert report['code']=='ETF_RECHECK_FAILED' and not report['publishReady']
    assert 'private' not in str(report)
    monkeypatch.setattr(sync,'preflight',original)
    assert sync.preflight(NOW)['pendingCount']==0


def test_unlock_error_keeps_http_and_releases_every_lock(new_pg_factory):
    sync,_,client,calls,_=setup(new_pg_factory)
    release=sync.lock.release
    released=[]
    def fail(key):
        released.append(key)
        release(key)
        raise RuntimeError('private secret')
    sync.lock.release=fail
    with client: report=sync.run(NOW)
    assert report['status']=='failed' and report['requestCount']==18 and len(calls)==18
    assert report['cleanupError']=='ETF_UNLOCK_FAILED'
    assert released==['sync-market-data','sync-stock-data:representative-50','sync-etf-heatmap']


def test_no_etf_checkpoints_keeps_unmigrated_ordinary_builder_read_compatible(new_pg_factory):
    sync,factory,_,_,mod=setup(new_pg_factory)
    MarketDailyQuote.__table__.drop(factory.kw['bind'])
    with factory() as session: assert mod.tiles(session,sync.catalog,DAY,NOW)==[]


def test_legitimate_core_revision_recollects_only_conflicting_daily_not_shares(new_pg_factory):
    sync,factory,client,calls,mod=setup(new_pg_factory)
    with client: assert sync.run(NOW)['status']=='completed'
    target=next(t for t in plan_market_targets(sync.catalog,NOW) if t.asset.asset_code==CODE)
    quote=TushareResponse(fields=QUOTE_FIELDS,items=((CODE,'20261016',4.236,4.236,4.236,4.236,4.242,-0.15),))
    assert MarketQuoteRepository(factory).persist(target,quote,captured_at=NOW)['status']=='completed'
    plan=sync.preflight(NOW)
    assert plan['pending']==[{'code':CODE,'api':'fund_daily'}] and not plan['publishReady']
    revised_calls=[]
    def revised(request):
        body=json.loads(request.content)
        revised_calls.append(body)
        data=response('fund_daily')
        values=list(data.items[0]); values[3]=-0.15
        return httpx.Response(200,json={'code':0,'data':{'fields':data.fields,'items':[values]}})
    with TushareClient('controlled',transport=httpx.MockTransport(revised),max_retries=0) as revised_client:
        sync.client=revised_client
        report=sync.run(NOW)
        assert report['status']=='completed' and report['requestCount']==1 and report['pendingCount']==0
        assert sync.run(NOW)['requestCount']==0
    assert len(revised_calls)==1 and revised_calls[0]['api_name']=='fund_daily'
