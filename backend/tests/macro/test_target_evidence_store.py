from backend.tests.macro.pg_evidence import evidence_fixture_transaction
from backend.tests.support.pg_snapshot import database_snapshot
"""真实PostgreSQL追加/重放及篡改边界，不用业务库或供应商。"""
from dataclasses import replace
from datetime import date, datetime
from importlib import import_module

import pytest
from sqlalchemy import inspect, select

from app.db.session import create_engine_and_session
from backend.tests.macro.test_target_evidence_normalize import request, response, CAPTURED, AS_OF
from backend.tests.macro.test_target_evidence_policy import policy


def implementation():
    try:
        return import_module('app.macro.target_evidence_store')
    except ImportError:
        pytest.fail('immutable target evidence store missing')


@pytest.fixture
def database(pg_factory):
    mod = implementation()
    models = import_module('app.db.models.enterprise_target_evidence')
    factory = pg_factory
    engine = factory.kw['bind']
    return mod.TargetEvidenceStore(factory), models, engine, factory


def seed(store):
    return store.receive(request('etf_basic', target='512880.SH', kind='etf'), response('etf_basic'), CAPTURED, policy())


def test_registered_postgres_metadata_passes_schema_gate(database):
    _, _, engine, factory = database
    from app.macro.target_evidence_schema import target_evidence_schema
    assert target_evidence_schema(factory) == (), {
        'columns': inspect(engine).get_columns('enterprise_target_candidate', schema='core'),
        'foreignKeys': inspect(engine).get_foreign_keys('enterprise_target_candidate', schema='core')}


def test_raw_replay_preserves_etf_identity_idempotence_and_readonly_database(database):
    store, models, engine, factory = database
    initial_tables = {(schema, name) for schema in ('core', 'raw', 'ops', 'public')
                      for name in inspect(engine).get_table_names(schema=schema)}
    key = seed(store)
    assert seed(store) == key
    # 数据库拒绝修改已登记的不可变原件，并回滚事务。
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError, match='TARGET_EVIDENCE_IMMUTABLE'), factory.begin() as session:
        session.get(models.TargetObservation, key).response_hash = '0' * 64
    before = database_snapshot(engine)
    engine.dispose()
    value = implementation().TargetEvidenceStore(factory).normalize(key, policy(), AS_OF, 'Asia/Shanghai')
    assert value['metadata']['ts_code'] == '512880.SH' and value['metadata']['index_code'] == '399975.SZ'
    assert value['capturedAt'] == CAPTURED.isoformat() and value['membershipReady'] is False
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 0}
    assert database_snapshot(engine) == before
    assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True) == value
    after = database_snapshot(engine)
    assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True) == value
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 1}
    assert database_snapshot(engine) == after
    assert initial_tables == {(schema, name) for schema in ('core', 'raw', 'ops', 'public')
                              for name in inspect(engine).get_table_names(schema=schema)}
    with factory() as session:
        assert session.scalar(select(models.TargetObservation)).target_kind == 'etf'


@pytest.mark.parametrize('bad', ['naive', 'row-limit', 'byte-limit', 'fields', 'nan', 'nested-scalar'])
def test_invalid_outer_response_never_creates_raw(database, bad):
    store, _, engine, _ = database
    p, captured, res = policy(), CAPTURED, response()
    if bad == 'naive': captured = datetime(2024, 8, 20)
    if bad == 'row-limit':
        p = replace(p, max_response_rows=1)
        res = response(rows=[('000300.SH', '000001.SZ', '20240820', 1)] * 2)
    if bad == 'byte-limit': p = replace(p, max_response_bytes=1)
    if bad == 'fields': res = response(fields=('index_code',))
    if bad == 'nan': res = response(rows=[('000300.SH', '000001.SZ', '20240820', float('nan'))])
    if bad == 'nested-scalar': res = response(rows=[('000300.SH', '000001.SZ', '20240820', {'weight': 1})])
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        store.receive(request(), res, captured, p)
    assert store.inventory() == {'observationCount': 0, 'candidateCount': 0} and database_snapshot(engine) == before


def test_invalid_business_row_is_retained_as_raw_then_rejected_candidate(database):
    store, _, _, _ = database
    key = store.receive(request(), response(rows=[('000300.SH', 'CASH', '20240820', -1)]), CAPTURED, policy())
    value = store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert value['qualityStatus'] == 'rejected' and value['snapshotCandidates'] == []
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 1}


@pytest.mark.parametrize('field', ['params', 'target_code', 'request_hash', 'response_hash', 'response', 'captured_at'])
def test_raw_tampering_blocks_replay_without_overwriting(database, field):
    store, models, engine, factory = database
    key = seed(store)
    with evidence_fixture_transaction(factory, models.TargetObservation) as session:
        row = session.get(models.TargetObservation, key)
        setattr(row, field, {'bad': 'private'} if field in ('params', 'response') else
            datetime(2024, 8, 21) if field == 'captured_at' else 'changed')
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize('field', ['payload', 'payload_hash', 'candidate_id', 'policy_hash', 'observation_id'])
def test_candidate_tampering_or_orphan_is_not_reported_as_valid(database, field):
    store, models, engine, factory = database
    key = seed(store)
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    if field == "observation_id":
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError), evidence_fixture_transaction(factory, models.TargetCandidate) as session:
            session.scalar(select(models.TargetCandidate)).observation_id = "b" * 64
        alternate = store.receive(request(), response(), CAPTURED, policy())
    with evidence_fixture_transaction(factory, models.TargetCandidate) as session:
        row = session.scalar(select(models.TargetCandidate))
        setattr(row, field, {'membershipReady': True} if field == 'payload' else alternate if field == 'observation_id' else 'b' * 64)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.inventory()
    assert database_snapshot(engine) == before


def test_candidate_identity_conflict_blocks_normalize_not_just_inventory(database):
    store, models, engine, factory = database
    key = seed(store)
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    with evidence_fixture_transaction(factory, models.TargetCandidate) as session:
        session.scalar(select(models.TargetCandidate)).candidate_id = 'c' * 64
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert database_snapshot(engine) == before


def test_policy_asof_and_algorithm_get_new_candidates_without_replacing_old(database, monkeypatch):
    store, _, _, _ = database
    key = seed(store)
    first = store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    store.normalize(key, replace(policy(), max_http_per_plan=13), AS_OF, 'Asia/Shanghai', persist=True)
    store.normalize(key, policy(), date(2024, 8, 19), 'Asia/Shanghai', persist=True)
    from app.macro import target_evidence_normalize
    monkeypatch.setattr(target_evidence_normalize, 'NORMALIZER_VERSION', 'controlled-next-normalizer')
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 4}
    monkeypatch.undo()
    assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai') == first


def test_missing_schema_gate_does_not_create_tables(pg_url):
    store_type = implementation().TargetEvidenceStore
    from app.macro.target_evidence_schema import target_evidence_schema
    engine, factory = create_engine_and_session(pg_url)
    try:
        before = database_snapshot(engine)
        assert len(target_evidence_schema(factory)) == 2
        with pytest.raises(ValueError, match='^TARGET_EVIDENCE_SCHEMA_REQUIRED$'):
            store_type(factory).receive(request(), response(), CAPTURED, policy())
        assert database_snapshot(engine) == before
        assert all(not inspect(engine).get_table_names(schema=schema)
                   for schema in ('public', 'core', 'raw', 'ops'))
    finally:
        engine.dispose()
