"""恢复预算独立、旧失败不变；真实SQLite而非模拟持久化。"""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_plan import config, payload, plan
from backend.tests.macro.test_extension_store import database, NOW, store, count


def recovery_config(tmp_path):
    raw = payload()
    for group in raw['groups'].values():
        group['collectionEnabled'] = True
    return config(tmp_path, raw)


def failed(factory, tmp_path, group='usdcnh', code='EXTENSION_RESPONSE_INVALID'):
    p = plan(tmp_path, group)
    parent = store(factory).reserve(p, NOW)
    store(factory).fail(parent, code, NOW + timedelta(seconds=1))
    return p, parent


def row_value(row):
    return deepcopy({c.name: getattr(row, c.name) for c in row.__table__.columns})


def recovery(factory, parent):
    from app.macro.extensions.recovery import RecoveryStore
    return RecoveryStore(factory, parent)


def test_recovery_reserves_once_without_mutating_original_failure_or_cursor(database, tmp_path):
    _, factory = database
    p, parent = failed(factory, tmp_path)
    with factory() as session:
        old = row_value(session.get(JobRun, parent))
        cursor = row_value(session.scalar(select(JobCursor)))
        batch = row_value(session.get(IngestionBatch, parent))
    r = recovery(factory, parent)
    assert r.recovery_plan(recovery_config(tmp_path), NOW + timedelta(days=2)) == p
    key = r.reserve(p, NOW + timedelta(seconds=2))
    assert key != parent
    assert r.inspect(p, NOW + timedelta(seconds=3))['status'] == 'uncertain'
    with pytest.raises(ExtensionError):
        r.reserve(p, NOW + timedelta(days=2))
    with factory() as session:
        assert row_value(session.get(JobRun, parent)) == old
        assert row_value(session.scalar(select(JobCursor))) == cursor
        assert row_value(session.get(IngestionBatch, parent)) == batch
        assert session.get(JobRun, key).request_count == 1
        assert session.get(JobRun, key).details['recoveryOf'] == parent
    assert count(factory, JobRun) == 2 and count(factory, SourcePayload) == 0


@pytest.mark.parametrize('group', ['usdcnh', 'nominalGdp'])
def test_received_recovery_replays_once_and_effective_normal_state_resumes(database, tmp_path, group):
    _, factory = database
    p, parent = failed(factory, tmp_path, group)
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    r.receive(key, fx() if group == 'usdcnh' else gdp(), NOW + timedelta(seconds=3))
    assert count(factory, MacroObservation) == 0
    newer = recovery(factory, parent)
    result = newer.replay(key, clock=lambda: NOW + timedelta(seconds=4))
    assert result['insertedRows'] == (1 if group == 'usdcnh' else 4)
    assert newer.replay(key)['insertedRows'] == 0
    state = store(factory).inspect(p, NOW + timedelta(seconds=5))
    assert state['status'] == 'completed' and state['runId'] == key
    with factory() as session:
        assert session.get(JobRun, parent).status == 'failed'
    later = plan(tmp_path, group, at='2026-10-30T20:00:00+08:00')
    assert store(factory).inspect(later, NOW + timedelta(days=9))['status'] == 'new'


@pytest.mark.parametrize('phase', ['reserved', 'failed'])
def test_uncertain_or_failed_recovery_cannot_retry_even_in_new_cycle(database, tmp_path, phase):
    _, factory = database
    p, parent = failed(factory, tmp_path)
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    if phase == 'failed':
        r.fail(key, 'EXTENSION_NETWORK_ERROR', NOW + timedelta(seconds=3))
    with pytest.raises(ExtensionError):
        r.reserve(p, NOW + timedelta(days=1))
    later = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    state = store(factory).inspect(later, NOW + timedelta(days=1))
    assert state['status'] == ('uncertain' if phase == 'reserved' else 'failed')
    assert state['runId'] == key and count(factory, JobRun) == 2


@pytest.mark.parametrize('code', ['EXTENSION_PERMISSION_ERROR', None])
def test_only_known_schema_failure_can_be_recovered(database, tmp_path, code):
    _, factory = database
    if code:
        p, parent = failed(factory, tmp_path, code=code)
    else:
        p = plan(tmp_path)
        parent = store(factory).reserve(p, NOW)
    with pytest.raises(ExtensionError):
        recovery(factory, parent).recovery_plan(recovery_config(tmp_path), NOW + timedelta(seconds=2))
    assert count(factory, JobRun) == 1


def test_parent_tampering_and_recovery_chain_are_rejected(database, tmp_path):
    _, factory = database
    p, parent = failed(factory, tmp_path)
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    r.fail(key, 'EXTENSION_RESPONSE_INVALID', NOW + timedelta(seconds=3))
    with pytest.raises(ExtensionError):
        recovery(factory, key).recovery_plan(recovery_config(tmp_path), NOW + timedelta(seconds=4))
    with factory.begin() as session:
        session.get(JobRun, parent).error_message = 'EXTENSION_PERMISSION_ERROR'
    with pytest.raises(ExtensionError):
        r.inspect(p, NOW + timedelta(seconds=4))
    with pytest.raises(ExtensionError):
        store(factory).inspect(p, NOW + timedelta(seconds=4))


def test_recovery_view_uses_completed_evidence_without_backdating_visibility(database, tmp_path):
    from app.macro.extensions.view import load_extension_inputs
    _, factory = database
    p, parent = failed(factory, tmp_path, 'nominalGdp')
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    r.receive(key, gdp(), NOW + timedelta(seconds=3))
    r.replay(key, clock=lambda: NOW + timedelta(seconds=10))
    earlier = load_extension_inputs(factory, NOW + timedelta(seconds=5))['sources']['nominalGdp']
    assert earlier['executionState'] == 'failed' and earlier['points'] == []
    later = load_extension_inputs(factory, NOW + timedelta(seconds=11))['sources']['nominalGdp']
    assert later['status'] == 'available' and later['executionState'] == 'completed'
    assert later['retrievedAt'] == (NOW + timedelta(seconds=3)).isoformat()
    assert [(p['period'], p['value']) for p in later['points']] == [
        ('2025Q1', '100'), ('2025Q2', '220'), ('2026Q1', '120'), ('2026Q2', '260')]


def test_completed_empty_recovery_is_missing_not_zero_or_old_schema_error(database, tmp_path):
    from app.macro.extensions.view import load_extension_inputs
    _, factory = database
    p, parent = failed(factory, tmp_path)
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    r.receive(key, {**fx(), 'items': []}, NOW + timedelta(seconds=3))
    r.replay(key, clock=lambda: NOW + timedelta(seconds=4))
    source = load_extension_inputs(factory, NOW + timedelta(seconds=5))['sources']['usdcnh']
    assert source['executionState'] == 'completed' and source['status'] == 'unavailable'
    assert source['reason'] == 'RESPONSE_EMPTY' and source['points'] == []
    assert source['retrievedAt'] is not None and source['evidenceFingerprint'] is not None


@pytest.mark.parametrize('corrupt', ['parent', 'child', 'raw'])
def test_recovery_view_rejects_changed_parent_child_or_raw(database, tmp_path, corrupt):
    from app.macro.extensions.view import load_extension_inputs
    _, factory = database
    p, parent = failed(factory, tmp_path)
    r = recovery(factory, parent)
    key = r.reserve(p, NOW + timedelta(seconds=2))
    r.receive(key, fx(), NOW + timedelta(seconds=3))
    r.replay(key, clock=lambda: NOW + timedelta(seconds=4))
    with factory.begin() as session:
        row = session.get(JobRun, key)
        if corrupt == 'parent':
            session.get(JobRun, parent).error_message = 'EXTENSION_PERMISSION_ERROR'
        elif corrupt == 'child':
            row.details = {**row.details, 'recoveryOf': '11111111-1111-4111-8111-111111111111'}
        else:
            session.get(SourcePayload, row.details['rawId']).content_hash = 'changed'
    with pytest.raises(ExtensionError):
        load_extension_inputs(factory, NOW + timedelta(seconds=5))


def test_recovery_rejects_policy_drift_without_creating_new_budget(database, tmp_path):
    from app.macro.extensions.config import ExtensionConfig
    _, factory = database
    _, parent = failed(factory, tmp_path)
    current = recovery_config(tmp_path).model_dump()
    current['groups']['usdcnh']['quoteSide'] = 'ask_close'
    with pytest.raises(ExtensionError):
        recovery(factory, parent).recovery_plan(ExtensionConfig.model_validate(current), NOW + timedelta(seconds=2))
    current['groups']['usdcnh'].update(quoteSide='bid_close', collectionEnabled=False)
    with pytest.raises(ExtensionError):
        recovery(factory, parent).recovery_plan(ExtensionConfig.model_validate(current), NOW + timedelta(seconds=2))
    assert count(factory, JobRun) == 1
