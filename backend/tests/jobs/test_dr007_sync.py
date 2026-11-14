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
from app.connectors.tushare.contracts import TushareResponse
from pydantic import SecretStr
from app.db.base import Base, ExternalBase, MartBase
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot, SourcePayload
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.jobs.dr007_history import Dr007HistoryJob, load_dr007_source
from app.macro.jobs.dr007_history_repository import Dr007HistoryRepository
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import MacroSeriesCatalog
from app.macro.history_catalog import ARCHIVED_V8_FILE

SID = 'cn.rate.dr007'
NBS = 'cn.cpi.core_yoy'
HISTORY_TIME = datetime(2026, 10, 7, 15, tzinfo=UTC)
NOW = HISTORY_TIME + timedelta(minutes=1)


class HistoryTransport:
    def call(self, api, params, fields):
        assert api == 'repo_daily' and params['ts_code'] == 'DR007.IB'
        period = datetime.strptime(params['start_date'], '%Y%m%d').date()
        end = period.replace(day=monthrange(period.year,period.month)[1])
        return TushareResponse(fields=tuple(fields),items=((end.strftime('%Y%m%d'),'DR007.IB','DR007',1.3665),))


def seed(factory, lock, *, end=None):
    catalog = MacroSeriesCatalog.load(ARCHIVED_V8_FILE)
    cursors = SqlJobCursorRepository(factory, job_name='backfill-dr007-history')
    job = Dr007HistoryJob(catalog=catalog, source=load_dr007_source(), transport=HistoryTransport(),
        cursors=cursors, repository=Dr007HistoryRepository(factory), lock=lock, clock=lambda: HISTORY_TIME)
    assert job.run(as_of=HISTORY_TIME, history_years=1, end=end, request_limit=100)['completedMonths'] == 12
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
    return cursors.load('dr007-history-v1')


def test_scoped_runtime_entry_exists():
    assert importlib.util.find_spec('app.macro.jobs.dr007_sync_runtime') is not None


@pytest.fixture
def database(tmp_path):
    if importlib.util.find_spec('app.macro.jobs.dr007_sync_runtime') is None:
        pytest.skip('scoped runtime not implemented yet')
    engine, factory = create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "sync.sqlite"}')
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    old = seed(factory, ProcessLocalJobLock())
    yield Settings(database_url=str(engine.url), macro_history_years=1, tushare_token=SecretStr('controlled-token'), tushare_standard_requests_per_minute=100000), factory, old
    engine.dispose()


class CurrentSource:
    def __init__(self, kind='success'):
        self.kind, self.calls = kind, []
    def handle(self, request):
        import json
        body=json.loads(request.content)
        assert request.method=='POST' and body['api_name']=='repo_daily'
        assert body['token']=='controlled-token'
        params=body['params']
        assert set(params)=={'ts_code','start_date','end_date'} and params['ts_code']=='DR007.IB'
        self.calls.append(params)
        if self.kind=='failed': return httpx.Response(503)
        rows=[] if self.kind=='empty' else [
            ['20260930','DR007.IB','DR007',1.3665],['20261006','DR007.IB','DR007',1.3822]]
        if self.kind=='expired': rows=[['20260927','DR007.IB','DR007',1.6]]
        if self.kind=='unchanged': rows=rows[:1]
        if self.kind=='long_success': rows=[[params['end_date'],'DR007.IB','DR007',1.3822]]
        rows=[row for row in rows if params['start_date']<=row[0]<=params['end_date']]
        fields=['trade_date','ts_code','repo_maturity','weight']
        if self.kind=='secret_column':
            fields=['trade_date','token','ts_code','repo_maturity','weight','unexpected']
            rows=[[row[0],'controlled-token',*row[1:],'unrequested-secret-canary'] for row in rows]
        return httpx.Response(200,json={'code':0,'msg':'','data':{
            'fields':fields,'items':rows}})


def execute(settings, source, *, as_of=NOW, before=None):
    module = importlib.import_module('app.macro.jobs.dr007_sync_runtime')
    with module.open_dr007_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
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
    assert [(q['start_date'], q['end_date']) for q in source.calls] == [('20260907', '20261007')]
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == old
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert len(session.scalars(select(MacroObservation)).all()) == 13
        state = session.get(MacroSeriesState, SID)
        assert state.active_source == 'tushare' and state.latest_observed_at == date(2026, 10, 6)
        assert state.catalog_version == '2026-10-08-v9' and state.stale is False
        assert session.get(MacroSeriesState, NBS).catalog_version == '2026-10-07-v8'
        failures = session.execute(select(JobFailure, JobRun).join(JobRun, JobFailure.job_run_id == JobRun.id)).all()
        for failure, run in failures:
            expected = failure.dataset == f'macro:{SID}' and failure.error_code == 'MACRO_NO_SOURCE' and run.details['asOf'] == HISTORY_TIME.isoformat()
            assert (failure.resolved_at is not None) == expected
        runs = session.scalars(select(JobRun).where(JobRun.job_name == 'sync-dr007')).all()
        assert len(runs) == 1 and runs[0].request_count == 1
        assert SqlMacroSyncHistory(factory).inspect(macro_sync_business_key(NOW,
            '2026-10-08-v9', 'Asia/Shanghai'), '2026-10-08-v9', runtime_hash(factory)) == (False, 1)


def runtime_hash(factory):
    with factory() as session:
        return session.scalar(select(JobRun).where(JobRun.job_name == 'sync-dr007')).details['catalogHash']


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
            MacroObservation.observed_at == date(2026, 9, 30))).value == Decimal('1.3665')


def test_current_success_keeps_old_cross_namespace_history_and_unknown_failures(database):
    settings, factory, _ = database
    with factory.begin() as session:
        old_run = session.scalar(select(JobRun).where(JobRun.job_name == 'sync-macro'))
        for code in ('MACRO_HISTORY_GAP', 'UNKNOWN_LEGACY_ERROR', 'TIMEOUT'):
            session.add(JobFailure(job_run_id=old_run.id, dataset=f'macro:{SID}', request_key='tushare',
                error_code=code, message=code, retryable=False, request_payload={}))
    report = execute(settings, CurrentSource())
    assert report.status == 'completed'
    with factory() as session:
        preserved = session.scalars(select(JobFailure).where(JobFailure.request_key == 'tushare')).all()
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
    settings,factory,old=database
    from app.macro.jobs import dr007_sync_runtime as module
    catalog=MacroSeriesCatalog.load(module.DEFAULT_MACRO_CATALOG_FILE)
    definition=catalog.require(SID)
    changed=definition.model_copy(update={'sources':(definition.sources[0].model_copy(
        update={'params':{'ts_code':'DR007.IB','maxPages':'1'}}),)})
    catalog=catalog.model_copy(update={'series':tuple(changed if d.id==SID else d for d in catalog.series)})
    monkeypatch.setattr(module,'load_active_catalog',lambda:catalog)
    source=CurrentSource()
    report=execute(settings,source,as_of=NOW.replace(month=11,day=8))
    assert report.status=='partial' and report.row_count==0 and report.request_count==0
    assert source.calls==[]
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==12
        assert session.get(MacroSeriesState,SID).last_success_at is None
        assert session.scalar(select(JobCursor)).cursor_value==old


@pytest.mark.parametrize('mutation', ['raw', 'core', 'partial', 'future', 'future_state', 'years', 'end', 'metadata', 'source'])
def test_preflight_rejects_unproven_activation_without_requests_or_writes(database, mutation, monkeypatch):
    settings, factory, old = database
    from app.macro.jobs import dr007_sync_runtime as module
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
    def block(runtime):
        assert runtime.job._lock.try_acquire(data_publication_lock_key())
    try:
        report = execute(settings, source, before=block)
    finally:
        ProcessLocalJobLock().release(data_publication_lock_key())
    assert report.status == 'locked' and source.calls == []
    with factory() as session:
        assert session.get(MacroSeriesState, SID).active_source is None


def test_scoped_options_forbid_automatic_publication():
    with pytest.raises(ValueError):
        MacroSyncOptions(as_of=NOW, history_years=1, catalog_version='v', catalog_hash='a' * 64,
            series_ids=(SID,))


def test_shutdown_current_slices_beyond_one_month_and_can_become_fresh(database):
    settings,factory,old=database
    source=CurrentSource('long_success')
    report=execute(settings,source,as_of=NOW.replace(month=11,day=8))
    assert report.status=='completed' and report.request_count==2
    assert [(p['start_date'],p['end_date']) for p in source.calls]==[
        ('20260930','20261030'),('20261031','20261108')]
    with factory() as session:
        assert session.get(MacroSeriesState,SID).latest_observed_at==date(2026,11,8)
        assert session.scalar(select(JobCursor)).cursor_value==old


def test_cleared_explicit_cutoff_fails_before_any_http_or_audit(tmp_path):
    engine,factory=create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "cutoff.sqlite"}')
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    try:
        old=seed(factory,ProcessLocalJobLock(),end=date(2026,8,31))
        settings=Settings(database_url=str(engine.url),macro_history_years=1,
            tushare_token=SecretStr('controlled-token'),tushare_standard_requests_per_minute=100000)
        source=CurrentSource()
        with factory() as session: count=len(session.scalars(select(JobRun)).all())
        with pytest.raises(ValueError,match='^DR007_HISTORY_CONFIG_MISMATCH$'):
            execute(settings,source)
        assert source.calls==[]
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all())==count
            assert session.scalar(select(JobCursor)).cursor_value==old
    finally:
        engine.dispose()


def test_schema_rejection_masks_unrequested_positional_columns_before_raw_commit(database):
    import json
    settings,factory,old=database
    report=execute(settings,CurrentSource('secret_column'))
    assert report.status=='partial' and report.row_count==0 and report.request_count==1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==12
        assert session.get(MacroSeriesState,SID).last_success_at is None
        assert session.scalar(select(JobCursor)).cursor_value==old
        run=session.scalar(select(JobRun).where(JobRun.job_name=='sync-dr007'))
        assert run.request_count==1
        raw=session.scalar(select(SourcePayload).order_by(SourcePayload.id.desc()))
        encoded=json.dumps(raw.payload)
        assert 'controlled-token' not in encoded
        assert 'unrequested-secret-canary' not in encoded
        response=raw.payload['data']['pages'][0]['response']
        assert response['fields']==['trade_date','token','ts_code','repo_maturity','weight','unexpected']
        assert response['items']==[
            ['20260930','***','DR007.IB','DR007',1.3665,'***'],
            ['20261006','***','DR007.IB','DR007',1.3822,'***']]
        assert all(f.resolved_at is None for f in session.scalars(select(JobFailure)))
