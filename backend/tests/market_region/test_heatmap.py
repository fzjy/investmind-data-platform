import hashlib
from datetime import date, datetime, UTC, timedelta

from sqlalchemy import select

from app.db.models import Security, DailyBasic, DailyPrice, SourcePayload, PublishedSnapshot
from app.stocks.derived.inputs import CoreSnapshot
from app.market.marts.market import MarketMartBuilder
from backend.tests.market_pulse.test_repository import CODE, DAY, NOW, _factory
from backend.tests.market_region.test_repository import setup, response

OTHER='000001.SZ'


def snapshot():
    securities=(Security(ts_code=CODE,symbol=CODE[:6],name='A',data={'area':'深圳'}),
        Security(ts_code=OTHER,symbol=OTHER[:6],name='B',data={'region':'深圳'}))
    prices=(DailyPrice(ts_code=CODE,trade_date=DAY,close=10,pct_chg=2,amount=100000),
        DailyPrice(ts_code=OTHER,trade_date=DAY,close=5,pct_chg=None,amount=None))
    basics=(DailyBasic(ts_code=CODE,trade_date=DAY,total_mv=10000),
        DailyBasic(ts_code=OTHER,trade_date=DAY,total_mv=20000))
    return CoreSnapshot(DAY,securities,prices,basics,(),{},(),(),(),(),(),())


def test_region_uses_raw_city_label_stable_id_and_known_only_mean():
    explorer=MarketMartBuilder().build(snapshot(),'test')[1].payload
    regions=explorer['heatmapData'].get('region',[])
    assert len(regions)==1, 'region category missing'
    region=regions[0]
    assert region['id']=='region:'+hashlib.sha256('深圳'.encode()).hexdigest()[:16]
    assert region['name']=='深圳' and region['stockCount']==2 and region['change']==2
    assert region['marketCap']==3 and region['turnover'] is None
    assert region['coverage']=={'change':1,'marketCap':2,'turnover':1,'total':2}
    member=next(row for row in region['stocks'] if row['symbol']==CODE)
    assert member['marketCap']==1 and member['turnover']==1
    assert explorer['regionMetadata']['known']==2 and explorer['regionMetadata']['unknown']==0
    assert explorer['regionMetadata']['capturedAtFrom'] is None


def test_corruption_conflict_and_missing_go_to_unknown_not_guessed_province():
    sample=snapshot()
    sample.securities[0].data={'area':'深圳','_regionLineage':{'bad':True}}
    sample.securities[1].data={'area':'深圳','region':'广东'}
    explorer=MarketMartBuilder().build(sample,'test')[1].payload
    assert explorer['heatmapData'].get('region',[{}])[0].get('id')=='region:unknown'
    unknown=explorer['heatmapData']['region'][0]
    assert unknown['name']=='未知地域' and unknown['stockCount']==2
    assert explorer['regionMetadata']['known']==0 and explorer['regionMetadata']['unknown']==2


def test_current_metadata_time_separate_from_price_date_and_explicit_map_controls_display():
    import inspect
    assert 'regions' in inspect.signature(MarketMartBuilder).parameters, 'verified regions injection missing'
    regions={CODE:{'label':'贵州','capturedAt':(NOW+timedelta(days=1)).isoformat()}}
    explorer=MarketMartBuilder(regions=regions).build(snapshot(),'test')[1].payload
    assert {r['name'] for r in explorer['heatmapData']['region']}=={'贵州','未知地域'}
    meta=explorer['regionMetadata']
    assert meta['asOf']=='2026-10-16' and meta['capturedAtFrom']==(NOW+timedelta(days=1)).isoformat()
    assert meta['known']==1 and meta['unknown']==1 and meta['total']==2
    assert '当前' in meta['basis'] and '非历史' in meta['basis'] and '城市' in meta['basis']


def test_region_runtime_replaces_owned_category_and_blocks_lost_known_coverage():
    from backend.tests.marts.test_market_runtime import rebuild, SELECTION, NOW as RUNTIME_NOW
    from backend.tests.marts.test_content_runtime import prepare, state
    factory=_factory()
    prepare(factory)
    with factory.begin() as session:
        session.get(Security,CODE).data={'area':'贵州'}
    assert rebuild(factory)['status']=='completed'
    with factory() as session:
        explorer=session.get(PublishedSnapshot,'market-explorer').payload
        assert len([r for r in explorer['heatmapCategories'] if r['id']=='region'])==1
        assert explorer['heatmapData']['region'][0]['stockCount']==1
    before=state(factory)
    assert rebuild(factory)['status']=='no_change' and state(factory)==before
    with factory.begin() as session: session.get(Security,CODE).data={}
    before=state(factory)
    result=rebuild(factory)
    assert result['code']=='MARKET_CLASSIFICATION_REGRESSION' and result['regressedCategory']=='region'
    assert state(factory)==before


def test_ordinary_builder_keeps_verified_region_and_no_fallback_on_damage():
    from app.connectors.tushare.contracts import TushareResponse
    from app.market.region.repository import RegionRepository, FIELDS
    from app.jobs.build_and_publish import BuildAndPublishOptions
    from backend.tests.jobs.test_build_and_publish_integration import _real_build_and_publish_job
    from backend.tests.marts.test_content_runtime import prepare
    factory=_factory()
    prepare(factory)
    now=datetime.now(UTC)
    assert RegionRepository(factory).persist(CODE,'controlled',TushareResponse(fields=FIELDS,items=((CODE,'贵州'),)),now)
    options=BuildAndPublishOptions(date(2026,8,24),(CODE,))
    assert _real_build_and_publish_job(factory).run(options).status=='completed'
    with factory() as session:
        sectors=session.get(PublishedSnapshot,'market-explorer').payload['heatmapData'].get('region',[])
        assert sectors and sectors[0]['name']=='贵州'
    from backend.tests.marts.test_content_runtime import state
    with factory.begin() as session: session.scalar(select(SourcePayload)).content_hash='bad'
    before=state(factory)
    result=_real_build_and_publish_job(factory).run(options)
    assert result.status=='failed' and 'market.region_coverage' in result.error
    assert state(factory)==before
