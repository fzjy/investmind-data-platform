import importlib
import hashlib
import json
from datetime import date, datetime, UTC

import pytest
from sqlalchemy import select, func

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import DailyBasic, Security, SourcePayload, JobCursor, IngestionBatch, RejectedRecord
from app.market.data.catalog import MarketDataCatalog


DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 15, 35, tzinfo=UTC)
CODE = '600519.SH'
FIELDS = ('ts_code', 'trade_date', 'limit_status')


def module():
    assert importlib.util.find_spec('app.market.pulse') is not None, 'pulse repository missing'
    return importlib.import_module('app.market.pulse.repository')


def setup(new_pg_factory):
    value = module()
    factory = new_pg_factory()
    with factory.begin() as session:
        session.add(Security(ts_code=CODE, symbol='600519', name='controlled'))
        session.flush()
        session.add(DailyBasic(ts_code=CODE, trade_date=DAY, pe=23, data={'pe': '23', 'other': {'a': 1}}))
    return value, factory, value.PulseRepository(factory)


def response(status=0, code=CODE, day='20261016', fields=FIELDS):
    return TushareResponse(fields=fields, items=((code, day, status),))


def test_status_zero_preserves_other_basic_fields(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    assert not repo.is_complete(CODE, DAY, 'fp', NOW)
    assert repo.persist(CODE, DAY, 'fp', response(), NOW)
    assert repo.is_complete(CODE, DAY, 'fp', NOW)
    with factory() as session:
        basic = session.scalar(select(DailyBasic))
        assert basic.pe == 23 and basic.data['pe'] == '23' and basic.data['other'] == {'a': 1}
        assert value.validated_statuses(session, (basic,), NOW) == {CODE: 0}
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 1
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 1


@pytest.mark.parametrize('bad', [response(None), response(True), response(2.0), response(7),
    response('2.5'), response(code='000001.SZ'), response(day='20261015'),
    response(fields=('ts_code', 'ts_code', 'limit_status')),
    TushareResponse(fields=FIELDS, items=()), TushareResponse(fields=FIELDS, items=((CODE, '20261016', 2),)*2)])
def test_rejects_wrong_identity_or_invalid_status(new_pg_factory, bad):
    _, factory, repo = setup(new_pg_factory)
    assert not repo.persist(CODE, DAY, 'fp', bad, NOW)
    assert not repo.is_complete(CODE, DAY, 'fp', NOW)
    with factory() as session:
        assert session.scalar(select(DailyBasic)).data == {'pe': '23', 'other': {'a': 1}}
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 1
        assert session.scalar(select(func.count()).select_from(RejectedRecord)) == 1
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 0


@pytest.mark.parametrize('damage', ['raw_content', 'raw_hash', 'batch', 'cursor', 'core', 'request'])
def test_corrupt_proof_is_not_complete_or_publishable(new_pg_factory, damage):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(CODE, DAY, 'fp', response(2), NOW)
    with factory.begin() as session:
        basic = session.scalar(select(DailyBasic))
        raw = session.scalar(select(SourcePayload))
        if damage == 'raw_content':
            raw.payload = {**raw.payload, 'items': [[CODE, '20261016', 3]]}
        elif damage == 'raw_hash':
            raw.content_hash = 'bad'
        elif damage == 'batch':
            session.scalar(select(IngestionBatch)).status = 'rejected'
        elif damage == 'cursor':
            session.delete(session.scalar(select(JobCursor)))
        elif damage == 'request':
            raw.request_hash = 'bad'
        else:
            basic.data = {**basic.data, 'limit_status': 5}
    assert not repo.is_complete(CODE, DAY, 'fp', NOW)
    with factory() as session:
        assert value.validated_statuses(session, session.scalars(select(DailyBasic)), NOW) == {}


def test_pulse_config_does_not_invalidate_quotes():
    catalog = MarketDataCatalog.load()
    raw = catalog.model_dump(mode='json', by_alias=True)
    raw.pop('pulse', None)
    raw.pop('region', None)
    raw.pop('etfHeatmap', None)
    raw.pop('commodities', None)
    raw.pop('sentiment', None)
    expected = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    raw['pulse'] = {'enabled': True, 'maxRequestsPerRun': 51, 'maxRetries': 0, 'timeoutSeconds': 20}
    try:
        changed = MarketDataCatalog.model_validate(raw)
    except ValueError:
        pytest.fail('pulse configuration missing')
    assert catalog.fingerprint == changed.fingerprint == expected
    assert changed.pulse.max_requests_per_run == 51


def test_old_ingestion_cannot_erase_owned_status_or_proof(new_pg_factory):
    from app.ingestion.repositories import IngestionRepository
    value, factory, repo = setup(new_pg_factory)
    repo.persist(CODE, DAY, 'fp', response(2), NOW)
    with factory.begin() as session:
        IngestionRepository(session).upsert(DailyBasic, {'ts_code': CODE, 'trade_date': DAY},
            {'pe': 24, 'data': {'pe': 24, 'limit_status': None}})
    assert repo.is_complete(CODE, DAY, 'fp', NOW)
    with factory() as session:
        basic = session.scalar(select(DailyBasic))
        assert basic.pe == 24 and basic.data['pe'] == 24
        assert value.validated_statuses(session, (basic,), NOW) == {CODE: 2}
