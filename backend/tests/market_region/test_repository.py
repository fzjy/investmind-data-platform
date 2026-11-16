import importlib
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import Security, SourcePayload, JobCursor, IngestionBatch, RejectedRecord
from app.ingestion.repositories import IngestionRepository
from backend.tests.market_pulse.test_repository import CODE, NOW


def module():
    assert importlib.util.find_spec('app.market.region') is not None, 'region repository missing'
    return importlib.import_module('app.market.region.repository')


def response(area=' 贵州 ', code=CODE, fields=('ts_code', 'area')):
    return TushareResponse(fields=fields, items=((code, area),))


def setup(new_pg_factory):
    value = module()
    factory = new_pg_factory()
    with factory.begin() as session:
        session.add(Security(ts_code=CODE, symbol='600519', name='controlled', data={'other': {'keep': 1}}))
    return value, factory, value.RegionRepository(factory)


def test_region_preserves_security_and_old_writer(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    assert not repo.is_complete(CODE, 'fp', NOW, 30)
    assert repo.persist(CODE, 'fp', response(), NOW)
    with factory.begin() as session:
        IngestionRepository(session).upsert(Security, {'ts_code': CODE}, {'name': 'updated', 'data': {'name': 'updated', 'area': None}})
    assert repo.is_complete(CODE, 'fp', NOW, 30)
    with factory() as session:
        security = session.get(Security, CODE)
        assert security.name == 'updated' and security.data['name'] == 'updated'
        assert security.data['area'] == '贵州'
        regions = value.valid_regions(session, (security,), NOW)
        assert regions[CODE]['label'] == '贵州' and regions[CODE]['capturedAt'] == NOW.isoformat()
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 1


@pytest.mark.parametrize('bad', [response(None), response(True), response(1), response(''), response('--'), response('未知'),
    response(code='000001.SZ'), response(fields=('ts_code', 'ts_code')),
    TushareResponse(fields=('ts_code','area'), items=()), TushareResponse(fields=('ts_code','area'), items=((CODE,'贵州'),)*2)])
def test_invalid_region_retains_raw_without_success_cursor(new_pg_factory, bad):
    _, factory, repo = setup(new_pg_factory)
    assert not repo.persist(CODE, 'fp', bad, NOW)
    assert not repo.is_complete(CODE, 'fp', NOW, 30)
    with factory() as session:
        assert session.get(Security, CODE).data == {'other': {'keep': 1}}
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 1
        assert session.scalar(select(func.count()).select_from(RejectedRecord)) == 1
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 0


@pytest.mark.parametrize('damage', ['raw_content','raw_hash','batch','cursor','core','request','future','fingerprint'])
def test_invalid_or_corrupt_proof_is_unknown(new_pg_factory, damage):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(CODE, 'fp', response(), NOW)
    with factory.begin() as session:
        security, raw = session.get(Security, CODE), session.scalar(select(SourcePayload))
        if damage == 'raw_content': raw.payload = {**raw.payload, 'items': [[CODE,'北京']]}
        elif damage == 'raw_hash': raw.content_hash = 'bad'
        elif damage == 'batch': session.scalar(select(IngestionBatch)).status = 'rejected'
        elif damage == 'cursor': session.delete(session.scalar(select(JobCursor)))
        elif damage == 'request': raw.request_hash = 'bad'
        elif damage == 'future': raw.captured_at = NOW + timedelta(seconds=1)
        elif damage == 'fingerprint': security.data = {**security.data, value.LINEAGE: {**security.data[value.LINEAGE], 'fingerprint':'bad'}}
        else: security.data = {**security.data, 'area': '北京'}
    assert not repo.is_complete(CODE, 'fp', NOW, 30)
    with factory() as session:
        assert value.valid_regions(session, session.scalars(select(Security)), NOW) == {}


def test_expired_metadata_is_not_complete_but_current_legacy_can_be_labeled(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(CODE, 'fp', response(), NOW)
    assert repo.is_complete(CODE, 'fp', NOW + timedelta(days=29), 30)
    assert not repo.is_complete(CODE, 'fp', NOW + timedelta(days=30), 30)
    with factory() as session:
        assert value.valid_regions(session, session.scalars(select(Security)), NOW + timedelta(days=30)) == {}
    with factory.begin() as session:
        security = session.get(Security, CODE)
        security.data = {'area': '深圳', 'region': '广东'}
    with factory() as session:
        assert value.valid_regions(session, session.scalars(select(Security)), NOW) == {}
    with factory.begin() as session:
        session.get(Security, CODE).data = {'region': '深圳'}
    with factory() as session:
        assert value.valid_regions(session, session.scalars(select(Security)), NOW) == {CODE: {'label':'深圳', 'capturedAt':None}}


def test_missing_security_cannot_be_created_by_region(new_pg_factory):
    _, factory, repo = setup(new_pg_factory)
    assert not repo.persist('000001.SZ', 'fp', response(code='000001.SZ'), NOW)
    with factory() as session:
        assert session.get(Security, '000001.SZ') is None


@pytest.mark.parametrize('old', [{'area':'深圳'},{'region':'深圳'},{'area':'深圳','region':'广东'},
    {'area':'深圳','region':'广东','_regionLineage':{'bad':True}}])
def test_old_writer_preserves_legacy_and_conflict_evidence(new_pg_factory, old):
    _,factory,_=setup(new_pg_factory)
    with factory.begin() as session:
        session.get(Security,CODE).data=old
    with factory.begin() as session:
        IngestionRepository(session).upsert(Security,{'ts_code':CODE},{'data':{'name':'new'}})
    with factory() as session:
        data=session.get(Security,CODE).data
        assert data=={**old,'name':'new'}


@pytest.mark.parametrize('bad_id',[{'bad':1},[1],True,'1',None,-1,0])
def test_malformed_source_id_is_unknown_without_database_error(new_pg_factory, bad_id):
    value,factory,repo=setup(new_pg_factory)
    repo.persist(CODE,'fp',response(),NOW)
    with factory.begin() as session:
        security=session.get(Security,CODE)
        security.data={**security.data,value.LINEAGE:{**security.data[value.LINEAGE],'sourcePayloadId':bad_id}}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(security,'data')  # bool True == int 1；强制写入真正的JSON损坏样本。
    assert not repo.is_complete(CODE,'fp',NOW,30)
    with factory() as session:
        assert value.valid_regions(session,session.scalars(select(Security)),NOW)=={}


def test_quote_and_pulse_identity_unchanged():
    from app.market.data.catalog import MarketDataCatalog, plan_market_targets
    from app.market.pulse.sync import PulseSync
    from backend.tests.market_pulse.test_sync import POOL
    catalog = MarketDataCatalog.load()
    changed = catalog.model_copy(update={'region':catalog.region.model_copy(update={'refresh_interval_days':7,'max_requests_per_run':1})})
    assert [target.identity for target in plan_market_targets(catalog,NOW)] == [target.identity for target in plan_market_targets(changed,NOW)]
    assert PulseSync(None,None,POOL,catalog,None).fingerprint == PulseSync(None,None,POOL,changed,None).fingerprint
