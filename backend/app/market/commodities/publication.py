from datetime import date
from app.market.commodities.coverage import commodity_coverage_regressed
from sqlalchemy import select

from app.db.models import PublishedSnapshot,JobCursor
from app.jobs.locks import data_publication_lock_key,job_lock_for_engine
from app.market.pulse.repository import PulseRepository
from app.market.commodities.contracts import JOB_NAME
from app.market.commodities.repository import package
from app.market.commodities.missing import reconcile_commodity_missing
from app.marts.contracts import MartCandidate,release_version
from app.marts.dto import validate_product_payload
from app.marts.publication import SnapshotPublisher

OWNED=('commodityCategories','commodityPeriods','commodityData','commodityMetadata')
PRODUCTS=('market-overview','market-explorer')


def market_package(session,catalog,day,now,previous=None):
    if session.scalar(select(JobCursor.id).where(JobCursor.job_name==JOB_NAME).limit(1)) is None: return None
    current=package(session,catalog,day,now)
    if current: return current
    if previous is None:
        base=session.get(PublishedSnapshot,'market-explorer')
        previous=base.payload if base else {}
    try:
        prior=date.fromisoformat(previous['commodityMetadata']['asOf'])
        if prior>=day: return None
        old=package(session,catalog,prior,now)
        return old if old and all(old[k]==previous.get(k) for k in OWNED) else None
    except (KeyError,ValueError,TypeError): return None




def publish_commodity_marts(factory,selection,catalog,*,checked_at,expected_as_of):
    report={'status':'blocked','requestCount':0,'promotedProducts':[],'asOf':expected_as_of.isoformat()}
    lock=job_lock_for_engine(factory.kw['bind']); key=data_publication_lock_key()
    if not lock.try_acquire(key): return {**report,'code':'COMMODITY_PUBLICATION_LOCK_BUSY'}
    try:
        with factory.begin() as session:
            bases={r.product:r for r in session.scalars(select(PublishedSnapshot).where(
                PublishedSnapshot.product.in_(PRODUCTS)).order_by(PublishedSnapshot.product).with_for_update())}
            if set(bases)!=set(PRODUCTS) or any(b.as_of.date()!=expected_as_of for b in bases.values()):
                return {**report,'code':'COMMODITY_SAME_DAY_MARKET_BASE_REQUIRED'}
            screener=session.get(PublishedSnapshot,'screener')
            if screener is None or screener.as_of.date()!=expected_as_of or PulseRepository(factory).target(selection,checked_at)!=expected_as_of:
                return {**report,'code':'COMMODITY_TARGET_CHANGED'}
            values=package(session,catalog,expected_as_of,checked_at)
            if values is None: return {**report,'code':'COMMODITY_SOURCE_INCOMPLETE'}
            candidates=[]
            for product in PRODUCTS:
                base=bases[product]
                payload={**base.payload,**values} if product=='market-explorer' else dict(base.payload)
                missing=reconcile_commodity_missing(payload.get('missingData',[]),values)
                payload['missingData']=list(missing)
                if validate_product_payload(product,payload): return {**report,'code':'COMMODITY_DTO_INVALID'}
                candidates.append(MartCandidate(product,base.as_of,payload,missing_data=missing,stale=base.stale,stale_reason=base.stale_reason))
            version=release_version(expected_as_of,candidates); report['version']=version
            if all(bases[c.product].payload==c.payload and bases[c.product].version==version for c in candidates):
                return {**report,'status':'no_change'}
            publisher=SnapshotPublisher(factory)
            for candidate in candidates:
                publisher._write_page_snapshot(session,candidate,version)
                publisher._switch_pointer(session,candidate,version)
            session.flush()
            return {**report,'status':'completed','promotedProducts':list(PRODUCTS)}
    finally: lock.release(key)
