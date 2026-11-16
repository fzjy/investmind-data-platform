"""真实 PostgreSQL证明对账的原子性、来源边界和零网络CLI。"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import main as cli
from app.config import Settings
from app.connectors.macro.nbs_history import NbsReleaseLocator
from app.connectors.macro.nbs_release_feed import INDEX
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_history_backfill import CURSOR_KEY, _Checkpoint, _Entry
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import ARCHIVED_V7_FILE

AS_OF = datetime(2026, 10, 7, tzinfo=UTC)
CATALOG = MacroSeriesCatalog.load(ARCHIVED_V7_FILE)
IDS = ('cn.cpi.core_yoy', 'cn.industry.value_added_yoy')
LEGACY_HASH = '7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde'


def seed_complete_history(factory):
    entries = []
    with factory.begin() as session:
        for sid in IDS:
            definition = CATALOG.require(sid)
            for number in range(12):
                year, zero_month = divmod(2025 * 12 + 8 + number, 12)
                period = date(year, zero_month + 1, 1)
                if sid == IDS[1] and period.month in (1, 2):
                    continue
                release_year, release_month = divmod(year * 12 + period.month, 12)
                released = datetime(release_year, release_month + 1, 9, tzinfo=UTC)
                suffix = '居民消费价格同比上涨0.8%' if sid == IDS[0] else '规模以上工业增加值增长6.0%'
                title = f'{year}年{period.month}月份{suffix}'
                url = INDEX + f'{released:%Y%m}/t{released:%Y%m%d}_{1966000 + number + (100 if sid == IDS[1] else 0)}.html'
                locator = NbsReleaseLocator(series_id=sid, title=title, url=url)
                entries.append(_Entry(release=locator, status='done', value='1.0', released_at=released))
                session.add(MacroObservation(source='nbs', series_id=sid, name=definition.name,
                    region=definition.region, frequency=definition.frequency, unit=definition.unit,
                    seasonal_adjustment=definition.seasonal_adjustment, observed_at=period,
                    released_at=released, revision='history-fixture', value=Decimal('1.0'), source_url=url))
            session.add(MacroSeriesState(series_id=sid, catalog_version=CATALOG.catalog_version,
                stale=True, stale_reason='missing', last_error_code='MACRO_HISTORY_GAP',
                consecutive_failures=3, last_checked_at=AS_OF - timedelta(days=7)))
        state = _Checkpoint(catalog_version=CATALOG.catalog_version, catalog_hash=LEGACY_HASH,
            start=date(2025, 9, 1), end=date(2026, 8, 31), as_of=AS_OF - timedelta(days=6),
            history_years=1, next_index_page=1, page_count=1, anchor_fingerprint='a' * 64,
            discovery_complete=True, entries=entries)
        cursor = state.model_dump(mode='json')
        session.add(JobCursor(job_name='backfill-macro-history', cursor_key=CURSOR_KEY, cursor_value=cursor))
        session.add(MacroSeriesState(series_id='us.cpi.yoy', catalog_version=CATALOG.catalog_version,
            stale=True, stale_reason='TIMEOUT', last_error_code='TIMEOUT', consecutive_failures=2))
    history = SqlMacroSyncHistory(factory)
    run_id = history.start('old-gap', AS_OF - timedelta(days=7), CATALOG.catalog_version, LEGACY_HASH, 'cli', 1)
    history.finish(run_id, 'partial', {'unresolvedGaps': {
        IDS[0]: [{'start': '2025-09-30', 'end': '2026-08-31'}] * 2,
        IDS[1]: [{'start': '2025-09-30', 'end': '2026-08-31'}],
        'us.cpi.yoy': [{'start': '2020-01-01', 'end': '2020-01-31'}],
    }})
    for sid in IDS:
        history.record_failure(run_id, sid, 'nbs', 'MACRO_HISTORY_GAP', 'gap', False)
    history.record_failure(run_id, IDS[0], 'nbs', 'TIMEOUT', 'timeout', True)
    with factory() as session:
        before = dict(session.get(JobRun, run_id).details)
    return cursor, run_id, before


@pytest.fixture
def storage(pg_factory, pg_schema_url, monkeypatch):
    url = pg_schema_url
    engine, factory = pg_factory.kw["bind"], pg_factory
    saved = seed_complete_history(factory)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url=url,
        macro_history_years=1, macro_backfill_end=date(2026, 8, 31)))
    monkeypatch.setattr(cli, '_macro_as_of', lambda *a: AS_OF)
    monkeypatch.setattr(cli.httpx, 'Client', lambda *a, **k: pytest.fail('reconcile constructed HTTP'))
    yield engine, factory, saved
    engine.dispose()


def invoke():
    return CliRunner().invoke(cli.app, ['reconcile-macro-history'])


def test_reconcile_updates_only_proven_gaps_and_keeps_cursor_old_audit_and_pages(storage):
    engine, factory, (cursor, old_id, old_details) = storage
    result = invoke()
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report['status'] == 'completed' and report['requestCount'] == report['rowCount'] == 0
    assert report['reconciledSeries'] == list(IDS)
    assert report['coverage'][IDS[0]]['expected'] == 12
    assert report['coverage'][IDS[1]]['expected'] == 10
    history = SqlMacroSyncHistory(factory)
    with factory() as session:
        audit_times = [(r.business_key, r.finished_at) for r in session.scalars(select(JobRun))]
    assert history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash) == {
        'us.cpi.yoy': ((date(2020, 1, 1), date(2020, 1, 31)),)
    }, audit_times
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == cursor
        assert session.get(JobRun, old_id).details == old_details
        assert len(session.scalars(select(MacroObservation)).all()) == 22
        assert session.scalars(select(PublishedSnapshot)).all() == []
        for sid in IDS:
            state = session.get(MacroSeriesState, sid)
            assert not state.stale and state.active_source == 'nbs' and state.last_error_code is None
            assert state.latest_observed_at == date(2026, 8, 1)
        assert session.get(MacroSeriesState, 'us.cpi.yoy').last_error_code == 'TIMEOUT'
        failures = session.scalars(select(JobFailure)).all()
        assert all((f.resolved_at is not None) == (f.error_code == 'MACRO_HISTORY_GAP') for f in failures)
        runs = session.scalars(select(JobRun)).all()
        assert len(runs) == 2
        new = next(r for r in runs if r.id != old_id)
        assert new.details['operation'] == 'nbs-history-reconciliation'
        assert new.details['sourceRunId'] == old_id
        assert new.details['currentUnresolvedGaps'] == new.details['unresolvedGaps']
        assert new.details['previousUnresolvedGaps'] == old_details['unresolvedGaps']
    again = invoke()
    assert again.exit_code == 0 and json.loads(again.stdout)['status'] == 'no_change'
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == 2


@pytest.mark.parametrize('fault', ['missing', 'value', 'expired', 'future', 'duplicate', 'unit', 'url', 'pending', 'hash', 'version', 'future_state', 'window'])
def test_incomplete_or_untrusted_proof_cannot_change_either_state_or_audit(storage, fault):
    engine, factory, saved = storage
    with factory.begin() as session:
        row = session.scalar(select(MacroObservation).where(MacroObservation.series_id == IDS[1]))
        cursor = session.scalar(select(JobCursor))
        raw = json.loads(json.dumps(cursor.cursor_value))
        if fault == 'missing':
            session.delete(row)
        elif fault == 'value':
            row.value = Decimal('99')
        elif fault == 'expired':
            row.valid_until = AS_OF - timedelta(days=1)
        elif fault == 'future':
            row.released_at = AS_OF + timedelta(days=1)
        elif fault == 'duplicate':
            session.add(MacroObservation(**{column.name: getattr(row, column.name) for column in row.__table__.columns if column.name not in {'id', 'revision'}}, revision='duplicate'))
        elif fault == 'unit':
            row.unit = 'wrong-unit'
        elif fault == 'url':
            row.source_url = 'https://evil.test/report'
        elif fault == 'pending':
            raw['entries'][0].update(status='pending', value=None, released_at=None)
        elif fault == 'hash':
            raw['catalog_hash'] = 'b' * 64
        elif fault == 'version':
            raw['catalog_version'] = 'unknown-version'
        elif fault == 'future_state':
            session.get(MacroSeriesState, IDS[1]).last_checked_at = AS_OF + timedelta(days=1)
        elif fault == 'window':
            run = session.get(JobRun, saved[1])
            run.details = {**run.details, 'unresolvedGaps': {IDS[0]: [{'start': '2020-01-01', 'end': '2026-08-31'}]}}
        cursor.cursor_value = raw
    result = invoke()
    assert result.exit_code == 3, result.output
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == 1
        assert all(session.get(MacroSeriesState, sid).last_error_code == 'MACRO_HISTORY_GAP' for sid in IDS)
        assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))


def test_lock_conflict_has_no_mutation(storage):
    engine, factory, saved = storage
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    try:
        result = invoke()
        assert result.exit_code == 5 and json.loads(result.stdout)['status'] == 'locked'
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 1
    finally:
        lock.release(data_publication_lock_key())


def test_transaction_failure_rolls_back_states_resolutions_and_new_snapshot(storage):
    engine, factory, saved = storage
    def fail_commit(session, context, instances):
        if any(isinstance(item, JobRun) and item.details.get('operation') == 'nbs-history-reconciliation' for item in session.new):
            raise RuntimeError('postgres://private-user:private-password@host/db')
    event.listen(Session, 'before_flush', fail_commit)
    try:
        result = invoke()
        assert result.exit_code == 3
        assert 'private-password' not in result.output
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 1
            assert all(session.get(MacroSeriesState, sid).stale for sid in IDS)
            assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))
    finally:
        event.remove(Session, 'before_flush', fail_commit)


def test_completed_history_is_not_claimed_fresh_after_a_long_shutdown(storage, monkeypatch):
    engine, factory, saved = storage
    monkeypatch.setattr(cli, '_macro_as_of', lambda *a: datetime(2027, 1, 1, tzinfo=UTC))
    result = invoke()
    assert result.exit_code == 0, result.output
    with factory() as session:
        for sid in IDS:
            state = session.get(MacroSeriesState, sid)
            assert state.stale and state.last_error_code == 'MACRO_VALUE_EXPIRED'
            assert state.last_success_at is None


def test_bad_done_core_outside_old_gap_cannot_be_used_as_latest_success(storage):
    engine, factory, saved = storage
    with factory.begin() as session:
        run = session.get(JobRun, saved[1])
        declared = {sid: [{'start': '2025-09-30', 'end': '2026-03-31'}] for sid in IDS}
        run.details = {**run.details, 'unresolvedGaps': declared}
        latest = session.scalar(select(MacroObservation).where(MacroObservation.series_id == IDS[0],
            MacroObservation.observed_at == date(2026, 8, 1)))
        latest.value = Decimal('99')
    result = invoke()
    assert result.exit_code == 3, result.output
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == 1
        assert session.get(MacroSeriesState, IDS[0]).last_error_code == 'MACRO_HISTORY_GAP'


@pytest.mark.parametrize('evidence', ['missing', 'invalid', 'outside', 'covered'])
def test_failure_resolution_requires_its_own_proven_months(storage, evidence):
    engine, factory, saved = storage
    with factory.begin() as session:
        details = {'asOf': (AS_OF - timedelta(days=8)).isoformat(),
            'catalogVersion': CATALOG.catalog_version, 'catalogHash': LEGACY_HASH}
        if evidence != 'missing':
            details['unresolvedGaps'] = {IDS[0]: [{'start': 'bad-date' if evidence == 'invalid' else
                '2020-01-01' if evidence == 'outside' else '2025-09-30', 'end': '2026-08-31'}]}
        session.add(JobRun(id='other-gap', job_name='sync-macro', business_key='other-gap', status='partial',
            started_at=AS_OF - timedelta(days=8), finished_at=AS_OF - timedelta(days=8), details=details))
        session.flush()
        session.add(JobFailure(job_run_id='other-gap', dataset=f'macro:{IDS[0]}', request_key='nbs',
            error_code='MACRO_HISTORY_GAP', message='gap', request_payload={}, retryable=False))
    result = invoke()
    assert result.exit_code == 0, result.output
    with factory() as session:
        failure = session.scalar(select(JobFailure).where(JobFailure.job_run_id == 'other-gap'))
        assert (failure.resolved_at is not None) == (evidence == 'covered')
        assert session.get(JobRun, 'other-gap').details == details


@pytest.mark.parametrize('hour, expired', [(15, False), (16, True)])
def test_expiry_uses_configured_business_timezone_at_midnight(storage, monkeypatch, hour, expired):
    engine, factory, saved = storage
    # 北京时间11-01 23:30仍在62天内；11-02 00:30已经63天。
    monkeypatch.setattr(cli, '_macro_as_of', lambda *a: datetime(2026, 11, 1, hour, 30, tzinfo=UTC))
    result = invoke()
    assert result.exit_code == 0, result.output
    with factory() as session:
        for sid in IDS:
            state = session.get(MacroSeriesState, sid)
            assert state.stale == expired
            assert (state.last_success_at is None) == expired
            assert state.last_error_code == ('MACRO_VALUE_EXPIRED' if expired else None)


@pytest.mark.parametrize('fault', ['future_observed', 'state_version'])
def test_untrusted_state_rejects_whole_transaction_without_overwriting(storage, fault):
    engine, factory, saved = storage
    with factory.begin() as session:
        state = session.get(MacroSeriesState, IDS[1])
        if fault == 'future_observed':
            state.latest_observed_at = AS_OF.date() + timedelta(days=1)
        else:
            state.catalog_version = 'different-version'
    with factory() as session:
        before = {sid: {c.name: getattr(session.get(MacroSeriesState, sid), c.name)
            for c in MacroSeriesState.__table__.columns} for sid in IDS}
    result = invoke()
    assert result.exit_code == 3, result.output
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == 1
        assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))
        assert {sid: {c.name: getattr(session.get(MacroSeriesState, sid), c.name)
            for c in MacroSeriesState.__table__.columns} for sid in IDS} == before


def test_conflicting_independent_timeout_state_is_not_cleared(storage):
    engine, factory, saved = storage
    with factory.begin() as session:
        state = session.get(MacroSeriesState, IDS[0])
        state.last_error_code, state.stale_reason = 'TIMEOUT', 'MACRO_HISTORY_GAP'
    with factory() as session:
        before = {c.name: getattr(session.get(MacroSeriesState, IDS[0]), c.name)
            for c in MacroSeriesState.__table__.columns}
    result = invoke()
    assert result.exit_code == 0, result.output
    with factory() as session:
        assert {c.name: getattr(session.get(MacroSeriesState, IDS[0]), c.name)
            for c in MacroSeriesState.__table__.columns} == before
        assert not session.get(MacroSeriesState, IDS[1]).stale
