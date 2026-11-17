"""真实PostgreSQL事务/唯一键，外部HTTP不在本测试层。"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_plan import plan, payload


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def database(pg_factory):
    return pg_factory.kw['bind'], pg_factory


def store(factory):
    from app.macro.extensions.store import ExtensionStore
    from app.macro.extensions.ingest import utc
    class ControlledStore(ExtensionStore):
        def replay(self, run_id, *, clock=None):
            if clock is None:
                # 本测试显式控制事件时间；正式实现默认读真实时钟，不倒写检索时间。
                with factory() as session:
                    row = session.get(JobRun, run_id)
                    raw = session.get(SourcePayload, row.details.get('rawId')) if row and row.details.get('rawId') else None
                    completed = utc(raw.captured_at) + timedelta(seconds=1) if raw else NOW
                clock = lambda: completed
            return super().replay(run_id, clock=clock)
    return ControlledStore(factory)


def count(factory, model):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_reservation_is_durable_unique_and_uncertain_even_next_period(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    assert store(factory).inspect(p, NOW)['status'] == 'new'
    key = store(factory).reserve(p, NOW)
    with factory() as session:
        job = session.get(JobRun, key)
        assert job.request_count == 1 and job.details['phase'] == 'reserved'
        assert session.get(IngestionBatch, key).request_count == 1
    assert count(factory, JobCursor) == 1
    assert store(factory).inspect(p, NOW)['status'] == 'uncertain'
    with pytest.raises(ExtensionError): store(factory).reserve(p, NOW)
    tomorrow = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    assert store(factory).inspect(tomorrow, NOW + timedelta(days=1))['status'] == 'uncertain'
    assert count(factory, JobRun) == 1 and count(factory, SourcePayload) == 0


def test_failed_request_is_not_refunded_or_retried_on_new_day(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).fail(key, 'EXTENSION_PERMISSION_ERROR', NOW + timedelta(seconds=1))
    assert store(factory).inspect(p, NOW + timedelta(seconds=2))['status'] == 'failed'
    tomorrow = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    with pytest.raises(ExtensionError): store(factory).reserve(tomorrow, NOW + timedelta(days=1))
    assert count(factory, JobRun) == 1


def test_raw_committed_before_core_and_new_instance_replays_once(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    assert count(factory, SourcePayload) == 1 and count(factory, MacroObservation) == 0
    assert store(factory).inspect(p, NOW + timedelta(seconds=2))['status'] == 'received'
    result = store(factory).replay(key)
    assert result['insertedRows'] == 1
    assert store(factory).inspect(p, NOW + timedelta(seconds=2))['status'] == 'completed'
    assert store(factory).replay(key)['insertedRows'] == 0
    assert count(factory, MacroObservation) == 1
    with factory() as session:
        row = session.scalar(select(MacroObservation))
        assert str(row.value) == '7.1234567890'
        assert row.series_id == 'fx.usdcnh' and row.unit == 'CNH/USD'
        raw = session.scalar(select(SourcePayload))
        assert raw.payload['releaseTimeBasis'] == 'retrieval-time-not-source-release'
        assert raw.payload['evidence']['observations'][0]['value'] == '7.123456789012345678'


def test_failed_core_transaction_keeps_raw_and_can_replay(database, tmp_path, monkeypatch):
    from app.macro.extensions import ingest
    _, factory = database
    p = plan(tmp_path, 'nominalGdp')
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, gdp(), NOW + timedelta(seconds=1))
    original = ingest.ingest_evidence
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('controlled')
    monkeypatch.setattr(ingest, 'ingest_evidence', fail_after_insert)
    with pytest.raises(RuntimeError): store(factory).replay(key)
    assert count(factory, SourcePayload) == 1 and count(factory, MacroObservation) == 0
    assert store(factory).inspect(p, NOW + timedelta(seconds=2))['status'] == 'received'
    monkeypatch.setattr(ingest, 'ingest_evidence', original)
    assert store(factory).replay(key)['insertedRows'] == 4
    assert count(factory, JobRun) == 1


def test_unchanged_next_period_does_not_duplicate_core_but_real_revision_closes_old(database, tmp_path):
    _, factory = database
    for offset, value in [(0, '7.1'), (1, '7.1'), (2, '7.2')]:
        at = NOW + timedelta(days=offset)
        p = plan(tmp_path, at=at.isoformat())
        key = store(factory).reserve(p, at)
        response = fx()
        response['items'][0][3:] = [value, '7.3']
        store(factory).receive(key, response, at + timedelta(seconds=1))
        assert store(factory).replay(key)['insertedRows'] == (0 if offset == 1 else 1)
    assert count(factory, MacroObservation) == 2 and count(factory, SourcePayload) == 3
    with factory() as session:
        rows = session.scalars(select(MacroObservation).order_by(MacroObservation.released_at)).all()
        assert rows[0].valid_until == rows[1].released_at
        assert rows[1].valid_until is None


def test_policy_drift_and_raw_tampering_block_instead_of_refetch(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    raw = payload()
    raw['groups']['usdcnh']['quoteSide'] = 'ask_close'
    with pytest.raises(ExtensionError): store(factory).inspect(plan(tmp_path, value=raw), NOW + timedelta(seconds=2))
    with factory.begin() as session:
        row = session.scalar(select(SourcePayload))
        row.content_hash = '0' * 64
    with pytest.raises(ExtensionError): store(factory).replay(key)
    assert count(factory, MacroObservation) == 0 and count(factory, JobRun) == 1


def test_received_previous_cycle_replays_before_next_cycle_and_clock_backwards_rejected(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    tomorrow = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    assert store(factory).inspect(tomorrow, NOW + timedelta(days=1))['runId'] == key
    with pytest.raises(ExtensionError): store(factory).inspect(p, NOW - timedelta(seconds=1))


def test_first_write_does_not_modify_unrelated_original_macro(database, tmp_path):
    _, factory = database
    with factory.begin() as session:
        session.add(MacroObservation(source='original', series_id='cn.gdp.real_yoy', name='old',
            region='CN', frequency='quarterly', unit='%', seasonal_adjustment='NSA',
            observed_at=NOW.date(), released_at=NOW, revision='old', value='5', source_url='old'))
    key = store(factory).reserve(plan(tmp_path), NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    store(factory).replay(key)
    with factory() as session:
        old = session.scalar(select(MacroObservation).where(MacroObservation.source == 'original'))
        assert old.revision == 'old' and old.valid_until is None and old.value == 5


def test_missing_root_blocks_raw_receive_and_core_replay(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    with factory.begin() as session:
        root = session.scalar(select(JobCursor))
        session.delete(root)
    with pytest.raises(ExtensionError): store(factory).replay(key)
    assert count(factory, MacroObservation) == 0


def test_core_storage_overflow_preserves_received_raw_without_partial_core(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    response = fx()
    response['items'][0][3:] = ['1e19', '2e19']
    store(factory).receive(key, response, NOW + timedelta(seconds=1))
    with pytest.raises(ExtensionError): store(factory).replay(key)
    assert count(factory, MacroObservation) == 0 and count(factory, SourcePayload) == 1


@pytest.mark.parametrize('damage', ['raw-hash', 'core-source', 'expired', 'inverted'])
def test_corrupt_completed_history_blocks_new_cycle_before_reservation(database, tmp_path, damage):
    _, factory = database
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    store(factory).replay(key)
    with factory.begin() as session:
        if damage == 'raw-hash': session.scalar(select(SourcePayload)).content_hash = '0' * 64
        else:
            row = session.scalar(select(MacroObservation))
            if damage == 'core-source': row.source = 'corrupted-source'
            else: row.valid_until = NOW + timedelta(seconds=0 if damage == 'inverted' else 1)
    tomorrow = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    with pytest.raises(ExtensionError): store(factory).reserve(tomorrow, NOW + timedelta(days=1))
    assert count(factory, JobRun) == 1


def test_unbound_wrong_source_target_is_not_hidden_by_filter(database, tmp_path):
    _, factory = database
    with factory.begin() as session:
        session.add(MacroObservation(source='wrong-provider', series_id='fx.usdcnh', name='bad',
            region='CN', frequency='daily', unit='CNH/USD', seasonal_adjustment='NSA',
            observed_at=NOW.date(), released_at=NOW, revision='bad', value='7', source_url='bad'))
    with pytest.raises(ExtensionError): store(factory).reserve(plan(tmp_path), NOW)
    assert count(factory, JobRun) == 0
