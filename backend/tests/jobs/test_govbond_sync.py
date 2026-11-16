"""完整受控历史到当前增量；HTTP替身之外保留真实runtime/raw/core/state。"""
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import importlib
import importlib.util
from calendar import monthrange
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.connectors.macro.transport import JsonTransportResponse
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot, SourcePayload
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.jobs.govbond_history import GovbondHistoryJob, load_govbond_source
from app.macro.jobs.govbond_history_repository import GovbondHistoryRepository
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import MacroSeriesCatalog
from app.macro.history_catalog import ARCHIVED_V7_FILE

SID = 'cn.rate.govbond_10y'
NBS = 'cn.cpi.core_yoy'
HISTORY_TIME = datetime(2026, 10, 7, 15, tzinfo=UTC)
NOW = HISTORY_TIME + timedelta(minutes=1)


def envelope(first, last, records, page=1):
    return {'head': {'version': '2.0', 'provider': 'CWAP', 'rep_code': '200'},
        'data': {'total': len(records), 'pageTotal': (len(records) + 14) // 15, 'pageNum': page - 1,
            'pageSize': 15, 'startDateCN': first, 'endDateCN': last, 'flagMessage': ''},
        'records': records[(page - 1) * 15:page * 15]}


class HistoryTransport:
    def post_json(self, url, payload):
        query = parse_qs(url.split('?', 1)[1])
        first, last = query['startDate'][0], query['endDate'][0]
        period = date.fromisoformat(first)
        end = period.replace(day=monthrange(period.year, period.month)[1])
        return JsonTransportResponse(envelope(first, last,
            [{'dateString': end.isoformat(), 'tenRate': '1.6750'}]), 1)


def seed(factory, lock):
    catalog = MacroSeriesCatalog.load(ARCHIVED_V7_FILE)
    cursors = SqlJobCursorRepository(factory, job_name='backfill-govbond-history')
    job = GovbondHistoryJob(catalog=catalog, source=load_govbond_source(), transport=HistoryTransport(),
        cursors=cursors, repository=GovbondHistoryRepository(factory), lock=lock, clock=lambda: HISTORY_TIME)
    assert job.run(as_of=HISTORY_TIME, history_years=1, end=None, request_limit=100)['completedMonths'] == 12
    with factory.begin() as session:
        session.add(MacroSeriesState(series_id=SID, catalog_version=catalog.catalog_version,
            active_source=None, latest_observed_at=None, latest_released_at=None,
            last_checked_at=HISTORY_TIME - timedelta(days=1), stale=True, stale_reason='missing',
            last_error_code='MACRO_NO_SOURCE', consecutive_failures=3, fallback_used=False))
        session.add(MacroSeriesState(series_id=NBS, catalog_version=catalog.catalog_version,
            active_source='nbs', latest_observed_at=date(2026, 8, 1), last_checked_at=HISTORY_TIME,
            stale=False, consecutive_failures=0, fallback_used=False))
        for sid, code, when in [(SID, 'MACRO_NO_SOURCE', HISTORY_TIME),
            (NBS, 'MACRO_NO_SOURCE', HISTORY_TIME), (SID, 'TIMEOUT', HISTORY_TIME),
            (SID, 'MACRO_NO_SOURCE', NOW + timedelta(days=1))]:
            run = JobRun(id=str(uuid4()), job_name='sync-macro', business_key=f'old-{sid}-{code}-{when.isoformat()}',
                status='partial', finished_at=HISTORY_TIME, details={'asOf': when.isoformat(),
                    'catalogVersion': catalog.catalog_version, 'catalogHash': catalog.catalog_hash})
            session.add(run)
            session.flush()
            session.add(JobFailure(job_run_id=run.id, dataset=f'macro:{sid}', request_key='unavailable',
                error_code=code, message=code, retryable=False, request_payload={}))
    return cursors.load('govbond-history-v1')


def test_scoped_runtime_entry_exists():
    assert importlib.util.find_spec('app.macro.jobs.govbond_sync_runtime') is not None


@pytest.fixture
def database(pg_factory):
    if importlib.util.find_spec('app.macro.jobs.govbond_sync_runtime') is None:
        pytest.skip('scoped runtime not implemented yet')
    engine, factory = pg_factory.kw["bind"], pg_factory
    old = seed(factory, PostgresAdvisoryJobLock(factory.kw["bind"]))
    yield Settings(database_url=engine.url.render_as_string(hide_password=False), macro_history_years=1), factory, old
    engine.dispose()


class CurrentSource:
    def __init__(self, kind='success'):
        self.kind, self.calls = kind, []
    def handle(self, request):
        assert request.method == 'POST' and request.url.path.endswith('/SddsIntrRateGovYldHis')
        query = dict(request.url.params)
        self.calls.append(query)
        if self.kind == 'failed':
            return httpx.Response(503)
        records = [] if self.kind == 'empty' else [
            {'dateString': '2026-09-30', 'tenRate': '1.6750'},
            {'dateString': '2026-10-06', 'tenRate': '1.7000'}]
        if self.kind == 'expired':
            records = [{'dateString': '2026-09-27', 'tenRate': '1.6'}]
        if self.kind == 'unchanged':
            records = records[:1]
        if self.kind == 'too_many':
            records = [{'dateString': f'2026-09-{day:02d}', 'tenRate': '1.6750'} for day in range(15, 31)]
        return httpx.Response(200, json=envelope(query['startDate'], query['endDate'], records))


def execute(settings, source, *, as_of=NOW, before=None):
    module = importlib.import_module('app.macro.jobs.govbond_sync_runtime')
    with module.open_govbond_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        runtime.job._clock = lambda: as_of
        if before:
            before(runtime)
        return runtime.job.run(MacroSyncOptions(as_of=as_of, history_years=settings.macro_history_years,
            catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
            mode='live', force=True, series_ids=(SID,), publish=False))


def test_scope_uses_verified_core_for_planning_without_publication(database):
    settings, factory, old = database
    source = CurrentSource()
    report = execute(settings, source)
    assert report.status == 'completed' and report.planned == 1 and report.row_count == 1
    assert report.publication is None and report.products == ()
    assert [(q['startDate'], q['endDate']) for q in source.calls] == [('2026-09-07', '2026-10-07')]
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == old
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert len(session.scalars(select(MacroObservation)).all()) == 13
        state = session.get(MacroSeriesState, SID)
        assert state.active_source == 'chinamoney' and state.latest_observed_at == date(2026, 10, 6)
        assert state.catalog_version == '2026-10-08-v9' and state.stale is False
        assert session.get(MacroSeriesState, NBS).catalog_version == '2026-09-30-v7'
        failures = session.execute(select(JobFailure, JobRun).join(JobRun, JobFailure.job_run_id == JobRun.id)).all()
        for failure, run in failures:
            expected = failure.dataset == f'macro:{SID}' and failure.error_code == 'MACRO_NO_SOURCE' and run.details['asOf'] == HISTORY_TIME.isoformat()
            assert (failure.resolved_at is not None) == expected
        runs = session.scalars(select(JobRun).where(JobRun.job_name == 'sync-govbond')).all()
        assert len(runs) == 1 and runs[0].request_count == 1
        assert SqlMacroSyncHistory(factory).inspect(macro_sync_business_key(NOW,
            '2026-10-08-v9', 'Asia/Shanghai'), '2026-10-08-v9', runtime_hash(factory)) == (False, 1)


def runtime_hash(factory):
    with factory() as session:
        return session.scalar(select(JobRun).where(JobRun.job_name == 'sync-govbond')).details['catalogHash']


@pytest.mark.parametrize('kind', ['failed', 'empty'])
def test_failed_or_empty_current_response_cannot_restore_missing_state(database, kind):
    settings, factory, old = database
    source = CurrentSource(kind)
    report = execute(settings, source)
    assert report.status == 'partial' and report.outcomes[0].status == 'stale'
    assert report.row_count == 0 and report.request_count == 1
    with factory() as session:
        state = session.get(MacroSeriesState, SID)
        assert state.stale and state.last_success_at is None
        assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))
        assert session.scalar(select(JobCursor)).cursor_value == old


def test_complete_unchanged_response_can_restore_real_existing_value(database):
    settings, factory, _ = database
    report = execute(settings, CurrentSource('unchanged'))
    assert report.status == 'completed' and report.row_count == 0
    with factory() as session:
        state = session.get(MacroSeriesState, SID)
        assert state.latest_observed_at == date(2026, 9, 30) and not state.stale
        row = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30)))
        assert row.released_at.replace(tzinfo=UTC) == HISTORY_TIME


def test_expired_response_cannot_borrow_newer_old_core_to_restore_success(database):
    settings, factory, old = database
    report = execute(settings, CurrentSource('expired'))
    assert report.status == 'partial' and report.outcomes[0].status == 'stale'
    assert report.outcomes[0].stale_reason == 'MACRO_VALUE_EXPIRED'
    with factory() as session:
        assert session.get(MacroSeriesState, SID).last_success_at.replace(tzinfo=UTC) == NOW
        assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))
        assert session.scalar(select(JobCursor)).cursor_value == old
        assert session.scalar(select(MacroObservation).where(
            MacroObservation.observed_at == date(2026, 9, 30))).value == Decimal('1.6750')


def test_current_success_keeps_old_cross_namespace_history_and_unknown_failures(database):
    settings, factory, _ = database
    with factory.begin() as session:
        old_run = session.scalar(select(JobRun).where(JobRun.job_name == 'sync-macro'))
        for code in ('MACRO_HISTORY_GAP', 'UNKNOWN_LEGACY_ERROR', 'TIMEOUT'):
            session.add(JobFailure(job_run_id=old_run.id, dataset=f'macro:{SID}', request_key='chinamoney',
                error_code=code, message=code, retryable=False, request_payload={}))
    report = execute(settings, CurrentSource())
    assert report.status == 'completed'
    with factory() as session:
        preserved = session.scalars(select(JobFailure).where(JobFailure.request_key == 'chinamoney')).all()
        assert len(preserved) == 3 and all(f.resolved_at is None for f in preserved)
        recovered = session.scalars(select(JobFailure).where(
            JobFailure.dataset == f'macro:{SID}', JobFailure.error_code == 'MACRO_NO_SOURCE')).all()
        assert sum(f.resolved_at is not None for f in recovered) == 1


def test_long_shutdown_does_not_claim_old_value_fresh(database):
    settings, factory, old = database
    checked_at = NOW.replace(month=11, day=8)
    report = execute(settings, CurrentSource('unchanged'), as_of=checked_at)
    assert report.status == 'partial' and report.outcomes[0].stale_reason == 'MACRO_VALUE_EXPIRED'
    assert report.row_count == 0
    with factory() as session:
        assert session.get(MacroSeriesState, SID).last_success_at.replace(tzinfo=UTC) == checked_at
        assert session.scalar(select(JobCursor)).cursor_value == old


def test_insufficient_page_budget_cannot_commit_partial_current_data(database, monkeypatch):
    settings, factory, old = database
    from app.macro.jobs import govbond_sync_runtime as module
    catalog = MacroSeriesCatalog.load(module.DEFAULT_MACRO_CATALOG_FILE)
    definition = catalog.require(SID)
    changed = definition.model_copy(update={'sources': (definition.sources[0].model_copy(update={'params': {'maxPages': '1'}}),)})
    catalog = catalog.model_copy(update={'series': tuple(changed if d.id == SID else d for d in catalog.series)})
    monkeypatch.setattr(module, 'load_active_catalog', lambda: catalog)
    source = CurrentSource('too_many')
    report = execute(settings, source)
    assert report.status == 'partial' and report.row_count == 0 and report.request_count == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 12
        assert session.get(MacroSeriesState, SID).last_success_at is None
        assert session.scalar(select(JobCursor)).cursor_value == old


@pytest.mark.parametrize('mutation', ['raw', 'core', 'partial', 'future', 'future_state', 'years', 'end', 'metadata', 'source'])
def test_preflight_rejects_unproven_activation_without_requests_or_writes(database, mutation, monkeypatch):
    settings, factory, old = database
    from app.macro.jobs import govbond_sync_runtime as module
    if mutation == 'future_state':
        with factory.begin() as session:
            session.get(MacroSeriesState, SID).last_checked_at = NOW + timedelta(days=1)
    if mutation in ('raw', 'core', 'partial'):
        with factory.begin() as session:
            if mutation == 'raw':
                session.scalar(select(SourcePayload)).content_hash = 'a' * 64
            elif mutation == 'core':
                session.scalar(select(MacroObservation)).value = Decimal('9')
            else:
                cursor = session.scalar(select(JobCursor))
                payload = deepcopy(cursor.cursor_value)
                entry = payload['entries'][0]
                entry.update(status='pending', batch_id=None, requested_at=None, captured_at=None, points=[])
                cursor.cursor_value = payload
    if mutation == 'years':
        settings = settings.model_copy(update={'macro_history_years': 2})
    if mutation == 'end':
        settings = settings.model_copy(update={'macro_backfill_end': date(2026, 8, 31)})
    if mutation in ('metadata', 'source'):
        catalog = MacroSeriesCatalog.load(module.DEFAULT_MACRO_CATALOG_FILE)
        definition = catalog.require(SID)
        updated = definition.model_copy(update={'unit': 'bp'}) if mutation == 'metadata' else definition.model_copy(
            update={'sources': (definition.sources[0].model_copy(update={'field': 'oneRate'}),)})
        catalog = catalog.model_copy(update={'series': tuple(updated if d.id == SID else d for d in catalog.series)})
        monkeypatch.setattr(module, 'load_active_catalog', lambda: catalog)
    source = CurrentSource()
    with factory() as session:
        count = len(session.scalars(select(JobRun)).all())
    with pytest.raises(ValueError):
        execute(settings, source, as_of=HISTORY_TIME - timedelta(seconds=1) if mutation == 'future' else NOW)
    assert source.calls == []
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == count
        assert session.get(MacroSeriesState, SID).active_source is None


def test_repeat_and_interrupt_after_core_are_idempotent(database, monkeypatch):
    settings, factory, old = database
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
    original = MacroSeriesStateRepository.mark_success
    monkeypatch.setattr(MacroSeriesStateRepository, 'mark_success', lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        execute(settings, CurrentSource())
    monkeypatch.setattr(MacroSeriesStateRepository, 'mark_success', original)
    report = execute(settings, CurrentSource())
    assert report.row_count == 0 and report.status == 'completed'
    assert execute(settings, CurrentSource()).row_count == 0
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 13
        assert session.scalar(select(JobCursor)).cursor_value == old


def test_shared_lock_has_no_http_or_state_mutation(database):
    settings, factory, _ = database
    source = CurrentSource()
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    assert lock.try_acquire(data_publication_lock_key())
    try:
        report = execute(settings, source)
    finally:
        lock.release(data_publication_lock_key())
    assert report.status == 'locked' and source.calls == []
    with factory() as session:
        assert session.get(MacroSeriesState, SID).active_source is None


def test_scoped_options_forbid_automatic_publication():
    with pytest.raises(ValueError):
        MacroSyncOptions(as_of=NOW, history_years=1, catalog_version='v', catalog_hash='a' * 64,
            series_ids=(SID,))
