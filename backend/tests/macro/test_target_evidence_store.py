"""真实SQLite追加/重放及篡改边界，不用业务库或供应商。"""
from dataclasses import replace
from datetime import date, datetime
from importlib import import_module

import pytest
from sqlalchemy import MetaData, inspect, select
from sqlalchemy.sql.schema import BLANK_SCHEMA

from app.db.session import create_engine_and_session
from backend.tests.macro.test_target_evidence_normalize import request, response, CAPTURED, AS_OF
from backend.tests.macro.test_target_evidence_policy import policy


def implementation():
    try:
        return import_module('app.macro.target_evidence_store')
    except ImportError:
        pytest.fail('immutable target evidence store missing')


@pytest.fixture
def database(tmp_path):
    mod = implementation()
    models = import_module('app.db.models.enterprise_target_evidence')
    from app.db.base import TargetEvidenceBase
    path = tmp_path / 'targets.sqlite'
    engine, factory = create_engine_and_session(f'sqlite:///{path.as_posix()}')
    # SQLite会省略跨schema FK；测试专用metadata去schema后保留真实FK。
    local_metadata = MetaData()
    for table in (models.TargetObservation.__table__,models.TargetCandidate.__table__):
        table.to_metadata(local_metadata, schema=None, referred_schema_fn=lambda *args: BLANK_SCHEMA)
    local_metadata.create_all(engine)
    try:
        yield mod.TargetEvidenceStore(factory), models, path, engine, factory
    finally:
        engine.dispose()


def seed(store):
    return store.receive(request('etf_basic', target='512880.SH', kind='etf'), response('etf_basic'), CAPTURED, policy())


def test_registered_sqlite_metadata_passes_schema_gate(database):
    _, _, _, engine, factory = database
    from app.macro.target_evidence_schema import target_evidence_schema
    assert target_evidence_schema(factory) == (), {
        'columns': inspect(engine).get_columns('enterprise_target_candidate'),
        'foreignKeys': inspect(engine).get_foreign_keys('enterprise_target_candidate')}


def test_raw_replay_preserves_etf_identity_idempotence_and_readonly_bytes(database):
    store, models, path, engine, factory = database
    key = seed(store)
    assert seed(store) == key
    before = path.read_bytes()
    engine.dispose()
    value = implementation().TargetEvidenceStore(factory).normalize(key, policy(), AS_OF, 'Asia/Shanghai')
    assert value['metadata']['ts_code'] == '512880.SH' and value['metadata']['index_code'] == '399975.SZ'
    assert value['capturedAt'] == CAPTURED.isoformat() and value['membershipReady'] is False
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 0}
    assert path.read_bytes() == before
    assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True) == value
    after = path.read_bytes()
    assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True) == value
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 1}
    assert path.read_bytes() == after
    assert set(inspect(engine).get_table_names()) == {'enterprise_target_observation', 'enterprise_target_candidate'}
    with factory() as session:
        assert session.scalar(select(models.TargetObservation)).target_kind == 'etf'


@pytest.mark.parametrize('bad', ['naive', 'row-limit', 'byte-limit', 'fields', 'nan', 'nested-scalar'])
def test_invalid_outer_response_never_creates_raw(database, bad):
    store, _, path, _, _ = database
    p, captured, res = policy(), CAPTURED, response()
    if bad == 'naive': captured = datetime(2024, 8, 20)
    if bad == 'row-limit':
        p = replace(p, max_response_rows=1)
        res = response(rows=[('000300.SH', '000001.SZ', '20240820', 1)] * 2)
    if bad == 'byte-limit': p = replace(p, max_response_bytes=1)
    if bad == 'fields': res = response(fields=('index_code',))
    if bad == 'nan': res = response(rows=[('000300.SH', '000001.SZ', '20240820', float('nan'))])
    if bad == 'nested-scalar': res = response(rows=[('000300.SH', '000001.SZ', '20240820', {'weight': 1})])
    before = path.read_bytes()
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        store.receive(request(), res, captured, p)
    assert store.inventory() == {'observationCount': 0, 'candidateCount': 0} and path.read_bytes() == before


def test_invalid_business_row_is_retained_as_raw_then_rejected_candidate(database):
    store, _, _, _, _ = database
    key = store.receive(request(), response(rows=[('000300.SH', 'CASH', '20240820', -1)]), CAPTURED, policy())
    value = store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert value['qualityStatus'] == 'rejected' and value['snapshotCandidates'] == []
    assert store.inventory() == {'observationCount': 1, 'candidateCount': 1}


@pytest.mark.parametrize('field', ['params', 'target_code', 'request_hash', 'response_hash', 'response', 'captured_at'])
def test_raw_tampering_blocks_replay_without_overwriting(database, field):
    store, models, path, _, factory = database
    key = seed(store)
    with factory.begin() as session:
        row = session.get(models.TargetObservation, key)
        setattr(row, field, {'bad': 'private'} if field in ('params', 'response') else
            datetime(2024, 8, 21) if field == 'captured_at' else 'changed')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize('field', ['payload', 'payload_hash', 'candidate_id', 'policy_hash', 'observation_id'])
def test_candidate_tampering_or_orphan_is_not_reported_as_valid(database, field):
    store, models, path, _, factory = database
    key = seed(store)
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    with factory.begin() as session:
        row = session.scalar(select(models.TargetCandidate))
        setattr(row, field, {'membershipReady': True} if field == 'payload' else 'b' * 64)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.inventory()
    assert path.read_bytes() == before


def test_candidate_identity_conflict_blocks_normalize_not_just_inventory(database):
    store, models, path, _, factory = database
    key = seed(store)
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    with factory.begin() as session:
        session.scalar(select(models.TargetCandidate)).candidate_id = 'c' * 64
    before = path.read_bytes()
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_(INVALID|CONFLICT)$'):
        store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    assert path.read_bytes() == before


def test_policy_asof_and_algorithm_get_new_candidates_without_replacing_old(database, monkeypatch):
    store, _, _, _, _ = database
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


def test_missing_schema_gate_does_not_create_tables(tmp_path):
    store_type = implementation().TargetEvidenceStore
    from app.macro.target_evidence_schema import target_evidence_schema
    engine, factory = create_engine_and_session(f'sqlite:///{(tmp_path / "empty.sqlite").as_posix()}')
    try:
        assert len(target_evidence_schema(factory)) == 2
        with pytest.raises(ValueError, match='^TARGET_EVIDENCE_SCHEMA_REQUIRED$'):
            store_type(factory).receive(request(), response(), CAPTURED, policy())
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()
