"""ETF专属子范围发布；其余模块逐字段原样保留。"""
from app.market.etf.coverage import etf_coverage_regressed
from sqlalchemy import select

from app.db.models import PublishedSnapshot
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.market.etf.repository import EtfRepository, tiles
from app.marts.contracts import MartCandidate, release_version
from app.marts.dto import validate_product_payload
from app.marts.publication import SnapshotPublisher

PRODUCTS=('market-overview','market-explorer')


def overlay(payload,etf_tiles):
    if not etf_tiles:
        return payload
    return {**payload,'heatmapCategories':[c for c in payload.get('heatmapCategories',[]) if c['id']!='etf']+
        [{'id':'etf','label':'ETF','total':len(etf_tiles)}],
        'heatmapData':{**payload.get('heatmapData',{}),'etf':etf_tiles}}




def publish_etf_marts(factory,selection,catalog,*,checked_at,expected_as_of):
    report={'status':'blocked','requestCount':0,'promotedProducts':[],'asOf':expected_as_of.isoformat()}
    lock=job_lock_for_engine(factory.kw['bind'])
    key=data_publication_lock_key()
    if not lock.try_acquire(key):
        return {**report,'code':'ETF_PUBLICATION_LOCK_BUSY'}
    try:
        with factory.begin() as session:
            bases={r.product:r for r in session.scalars(select(PublishedSnapshot).where(
                PublishedSnapshot.product.in_(PRODUCTS)).order_by(PublishedSnapshot.product).with_for_update())}
            if set(bases)!=set(PRODUCTS) or any(b.as_of.date()!=expected_as_of for b in bases.values()):
                return {**report,'code':'ETF_SAME_DAY_MARKET_BASE_REQUIRED'}
            # 股票源锁由收集器持有；在事务内再核验发布目标。
            screener=session.get(PublishedSnapshot,'screener')
            if screener is None or screener.as_of.date()!=expected_as_of or EtfRepository(factory).target(selection,checked_at)!=expected_as_of:
                return {**report,'code':'ETF_TARGET_CHANGED'}
            values=tiles(session,catalog,expected_as_of,checked_at)
            if len(values)!=9:
                return {**report,'code':'ETF_SOURCE_INCOMPLETE'}
            if bases['market-explorer'].payload.get('amountUnit')!='yi-yuan':
                return {**report,'code':'ETF_BASE_UNIT_REQUIRED'}
            candidates=[]
            for product in PRODUCTS:
                base=bases[product]
                payload=overlay(base.payload,values) if product=='market-explorer' else dict(base.payload)
                if validate_product_payload(product,payload):
                    return {**report,'code':'ETF_DTO_INVALID'}
                candidates.append(MartCandidate(product,base.as_of,payload,
                    missing_data=tuple(payload.get('missingData',[])),stale=base.stale,stale_reason=base.stale_reason))
            version=release_version(expected_as_of,candidates)
            report['version']=version
            if all(bases[c.product].payload==c.payload and bases[c.product].version==version for c in candidates):
                return {**report,'status':'no_change'}
            publisher=SnapshotPublisher(factory)
            for candidate in candidates:
                publisher._write_page_snapshot(session,candidate,version)
                publisher._switch_pointer(session,candidate,version)
            session.flush()
            return {**report,'status':'completed','promotedProducts':list(PRODUCTS)}
    finally:
        lock.release(key)
