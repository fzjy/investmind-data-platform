import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import SourcePayload, JobCursor, IngestionBatch
from backend.tests.market_pulse.test_sync import NOW,DAY,POOL,_factory


def mod(name='repository'):
    assert importlib.util.find_spec('app.market.commodities') is not None, 'commodity group missing'
    return importlib.import_module('app.market.commodities.'+name)


def setup():
    contracts=mod('contracts')
    from app.market.data.catalog import MarketDataCatalog
    catalog=MarketDataCatalog.load()
    catalog=catalog.model_copy(update={'commodities':catalog.commodities.model_copy(update={'enabled':True,'history_days':5})})
    return contracts,catalog,_factory()


def response(request):
    api,params=request['api'],request['params']
    days=[DAY-timedelta(days=4-i) for i in range(5)]
    if api=='fut_trade_cal': rows=[(params['exchange'],d.strftime('%Y%m%d'),1) for d in days]
    elif api=='fut_basic':
        product=params['fut_code']
        suffix={'SHFE':'SHF','DCE':'DCE','CZCE':'ZCE','INE':'INE'}[params['exchange']]
        rows=[(f'{product}{month}.{suffix}',product,params['exchange'],product+month,'吨','人民币元/吨',10,'20250101',end)
            for month,end in [('2610','20261015'),('2612','20261215')]]
    elif api=='fut_mapping':
        product,suffix=params['ts_code'].split('.')
        rows=[(params['ts_code'],d.strftime('%Y%m%d'),f'{product}{"2610" if i<2 else "2612"}.{suffix}') for i,d in enumerate(days)]
    else:
        rows=[(params['ts_code'],d.strftime('%Y%m%d'),99,100,100+i,130+i,90+i,120+i,110+i,0,Decimal('10000.5'),0)
            for i,d in enumerate(days) if '2610.' not in params['ts_code'] or d<=DAY-timedelta(days=1)]
    return TushareResponse(fields=mod('contracts').FIELDS[api],items=tuple(rows))


def complete(factory,catalog):
    repository=mod()
    repo=repository.CommodityRepository(factory,catalog)
    for _ in range(100):
        plan=repo.plan(DAY,NOW)
        if not plan['pendingRequests']: return plan
        for request in plan['pendingRequests']:
            assert repo.persist(request,response(request),NOW) is None
    raise AssertionError('bounded source graph failed to finish')


def test_real_mapping_dates_units_and_partial_aggregation():
    _,catalog,factory=setup()
    plan=complete(factory,catalog)
    assert plan['ready'] and plan['pendingRequests']==[]
    with factory() as session: package=mod().package(session,catalog,DAY,NOW)
    assert package['commodityMetadata']['instrumentCount']==10
    gold=package['commodityData']['precious']['domestic'][0]
    assert gold['code']=='AU.SHF' and gold['activeContract']=='AU2612.SHF'
    assert gold['change']==pytest.approx((124/123-1)*100) and gold['quoteUnit']=='人民币元/吨'
    assert gold['changeBasis']=='close/previous_series_close-1'
    assert gold['bars']['daily'][0]['contract']=='AU2610.SHF'
    week=gold['bars']['weekly'][0]
    assert (week['open'],week['high'],week['low'],week['close'])==(100,134,90,124)
    assert week['amount']==50002.5 and week['volume']==0
    assert week['rollover'] and week['partial']
    assert week['contracts']==['AU2610.SHF','AU2612.SHF']
    assert gold['bars']['monthly'][0]['partial']
    assert gold['bars']['intraday']==[]


@pytest.mark.parametrize('damage',['wrong_code','duplicate','outside','ohlc','negative','bool','nan','missing_date','saturated'])
def test_bad_mapping_or_daily_never_completes(damage):
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    request=next(r for r in repo.plan(DAY,NOW)['pendingRequests'] if r['api']=='fut_mapping')
    good=response(request)
    rows=[list(row) for row in good.items]
    if damage=='wrong_code': rows[0][2]='WRONG2610.SHF'
    elif damage=='duplicate': rows.append(rows[0])
    elif damage=='outside': rows[0][1]='20261011'
    elif damage=='missing_date': rows.pop()
    elif damage=='saturated': rows=rows*400
    else:
        request=mod('contracts').request('fut_daily',{'ts_code':'AU2612.SHF','start_date':'20261012','end_date':'20261016'},catalog,DAY)
        good=response(request); rows=[list(row) for row in good.items]
        if damage=='ohlc': rows[0][5]=80
        elif damage=='negative': rows[0][10]=-1
        elif damage=='bool': rows[0][7]=True
        else: rows[0][7]='NaN'
    bad=TushareResponse(fields=good.fields,items=tuple(tuple(row) for row in rows))
    assert repo.persist(request,bad,NOW)=='COMMODITY_RESPONSE_REJECTED'
    assert repo.read(request,NOW) is None


@pytest.mark.parametrize('damage',['hash','past_time','cursor_time','batch_time','cursor_bool','params'])
def test_damaged_raw_time_or_request_never_publishes(damage):
    _,catalog,factory=setup()
    complete(factory,catalog)
    with factory.begin() as session:
        cursor=session.scalar(select(JobCursor).where(JobCursor.job_name=='sync-commodities'))
        raw=session.get(SourcePayload,cursor.cursor_value['sourcePayloadId'])
        if damage=='hash': raw.content_hash='bad'
        elif damage=='past_time': raw.captured_at=NOW-timedelta(days=30)
        elif damage=='cursor_time': cursor.updated_at=NOW-timedelta(seconds=1)
        elif damage=='batch_time': session.get(IngestionBatch,raw.batch_id).finished_at=NOW-timedelta(seconds=1)
        elif damage=='cursor_bool': cursor.cursor_value={**cursor.cursor_value,'sourcePayloadId':True}
        else: raw.request_hash='bad'
    with factory() as session: assert mod().package(session,catalog,DAY,NOW) is None


def test_metadata_delisting_and_missing_calendar_days_block_package():
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    complete(factory,catalog)
    request=next(r for r in mod('contracts').source_requests(catalog,DAY) if r['api']=='fut_basic')
    data=response(request); rows=[list(row) for row in data.items]; rows[1][-1]='20261013'
    assert repo.persist(request,TushareResponse(fields=data.fields,items=tuple(map(tuple,rows))),NOW) is None
    with factory() as session: assert mod().package(session,catalog,DAY,NOW) is None


def test_budget_and_timeout_change_do_not_invalidate_any_checkpoint_or_quotes():
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    complete(factory,catalog)
    changed=catalog.model_copy(update={'commodities':catalog.commodities.model_copy(update={'max_requests_per_run':1,'timeout_seconds':30})})
    assert changed.fingerprint==catalog.fingerprint
    assert mod().CommodityRepository(factory,changed).plan(DAY,NOW)['ready']


def test_non_main_delivery_ohlc_is_not_required_but_actual_main_ohlc_is_strict():
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    for req in repo.plan(DAY,NOW)['pendingRequests']: assert repo.persist(req,response(req),NOW) is None
    req=next(r for r in repo.plan(DAY,NOW)['pendingRequests'] if r['api']=='fut_daily' and r['params']['ts_code']=='AU2610.SHF')
    data=response(req); rows=[list(row) for row in data.items]
    # 10-15已不担任主力；类似真实交割记录不提供OHLC，不能污染10-12/13主力日。
    rows[-1][4]=rows[-1][5]=rows[-1][6]=0
    assert repo.persist(req,TushareResponse(fields=data.fields,items=tuple(map(tuple,rows))),NOW) is None
    assert set(repo.read(req,NOW)['rows'])=={'2026-10-12','2026-10-13'}
    rows[0][4]=rows[0][5]=rows[0][6]=0
    assert repo.persist(req,TushareResponse(fields=data.fields,items=tuple(map(tuple,rows))),NOW)=='COMMODITY_RESPONSE_REJECTED'


def test_three_digit_czce_contract_uses_metadata_lifecycle_not_guessed_year():
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    for _ in range(3):
        for req in repo.plan(DAY,NOW)['pendingRequests']:
            data=response(req)
            rows=tuple(tuple(v.replace('TA2610','TA610').replace('TA2612','TA612') if isinstance(v,str) else v for v in row) for row in data.items)
            assert repo.persist(req,TushareResponse(fields=data.fields,items=rows),NOW) is None
    assert repo.plan(DAY,NOW)['ready']
    with factory() as session: package=mod().package(session,catalog,DAY,NOW)
    pta=next(r for r in package['commodityData']['energy']['domestic'] if r['code']=='TA.ZCE')
    assert pta['activeContract']=='TA612.ZCE'
    assert pta['bars']['daily'][0]['contract']=='TA610.ZCE'
    req=next(r for r in repo.plan(DAY,NOW)['requests'] if r['api']=='fut_basic' and r['params']['fut_code']=='TA')
    data=response(req); rows=[list(row) for row in data.items]
    for row in rows: row[0]=row[0].replace('TA2610','TA610').replace('TA2612','TA612')
    rows[1][-1]='20261013'
    assert repo.persist(req,TushareResponse(fields=data.fields,items=tuple(map(tuple,rows))),NOW) is None
    assert not repo.plan(DAY,NOW)['ready']


def test_exchange_closed_day_needs_no_mapping_or_invented_candle():
    _,catalog,factory=setup()
    repo=mod().CommodityRepository(factory,catalog)
    for _ in range(3):
        for req in repo.plan(DAY,NOW)['pendingRequests']:
            data=response(req); rows=[list(row) for row in data.items]
            if req['api']=='fut_trade_cal': rows[2][2]=0
            elif req['api']=='fut_mapping': rows.pop(2)
            assert repo.persist(req,TushareResponse(fields=data.fields,items=tuple(map(tuple,rows))),NOW) is None
    assert repo.plan(DAY,NOW)['ready']
    with factory() as session: package=mod().package(session,catalog,DAY,NOW)
    assert [period['id'] for period in package['commodityPeriods']] == ['daily', 'weekly', 'monthly']
    gold=package['commodityData']['precious']['domestic'][0]
    assert [r['date'] for r in gold['bars']['daily']]==['2026-10-12','2026-10-13','2026-10-15','2026-10-16']


def test_cross_year_week_and_full_month_have_hand_checked_period_boundaries():
    from datetime import date
    aggregate=mod('aggregation').aggregate
    def candle(day,contract,close):
        return {'date':day,'contract':contract,'open':100,'high':130,'low':90,'close':close,'volume':2,'amount':3,'openInterest':4}
    week=aggregate([candle('2025-12-31','TA601.ZCE',110),candle('2026-01-02','TA605.ZCE',120)],'weekly',date(2025,12,29),date(2026,1,4))[0]
    assert (week['periodStart'],week['periodEnd'],week['partial'],week['rollover'])==('2025-12-29','2026-01-04',False,True)
    assert (week['open'],week['high'],week['low'],week['close'],week['volume'],week['amount'])==(100,130,90,120,4,6)
    rows=[candle('2026-01-02','TA605.ZCE',110),candle('2026-01-30','TA605.ZCE',120)]
    month=aggregate(rows,'monthly',date(2026,1,1),date(2026,1,31))[0]
    assert (month['periodStart'],month['periodEnd'],month['partial'],month['rollover'])==('2026-01-01','2026-01-31',False,False)
    assert aggregate(rows,'monthly',date(2026,1,2),date(2026,1,31))[0]['partial']
    assert aggregate(rows,'monthly',date(2026,1,1),date(2026,1,30))[0]['partial']


def test_close_basis_dto_and_legacy_snapshot_remain_distinct():
    from copy import deepcopy
    _,catalog,factory=setup()
    complete(factory,catalog)
    with factory() as session: payload=mod().package(session,catalog,DAY,NOW)
    valid=mod('dto').valid_commodity_payload
    assert valid(payload)
    gold=payload['commodityData']['precious']['domestic'][0]
    wrong=deepcopy(payload)
    wrong['commodityData']['precious']['domestic'][0]['change']=24
    assert not valid(wrong)
    legacy=deepcopy(payload)
    for group in legacy['commodityData'].values():
        for row in group['domestic']:
            row['changeBasis']='close/pre_settle-1'
            row['change']=24
    assert valid(legacy)
    # 一条日线没有比较基准，不伪造0或回退昨结算。
    gold['bars']['daily']=gold['bars']['daily'][-1:]
    gold['change']=None
    assert valid(payload)
    gold['change']=0
    assert not valid(payload)
