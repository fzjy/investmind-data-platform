"""真实 runtime/CLI/数据库，仅替换外部 HTTP，验证 NBS 限定增量。"""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import importlib
import importlib.util
import json

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.config import Settings
from app.db.base import Base, ExternalBase, MartBase
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot, SourcePayload
from app.db.session import create_engine_and_session
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from backend.tests.connectors.macro.test_nbs_feed import INDEX, CPI, CPI_TITLE, CPI_URL, INDUSTRY, INDUSTRY_TITLE, INDUSTRY_URL, index


IDS = ('cn.cpi.core_yoy', 'cn.industry.value_added_yoy')
NOW = datetime(2026, 10, 11, 0, 10, tzinfo=UTC)
OLD = datetime(2026, 9, 30, 10, tzinfo=UTC)
OTHER = 'us.rate.treasury_10y'


def module():
    assert importlib.util.find_spec('app.macro.jobs.nbs_sync_runtime') is not None, 'missing scoped NBS runtime'
    return importlib.import_module('app.macro.jobs.nbs_sync_runtime')


def seed(factory):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for sid, value, released in zip(IDS, ('1.0', '5.2'),
                (datetime(2026, 8, 9, 1, 30, tzinfo=UTC), datetime(2026, 8, 15, 2, tzinfo=UTC))):
            definition = catalog.require(sid)
            session.add(MacroObservation(source='nbs', series_id=sid, name=definition.name,
                region=definition.region, frequency=definition.frequency, unit=definition.unit,
                seasonal_adjustment=definition.seasonal_adjustment, observed_at=date(2026, 7, 1),
                released_at=released, revision='controlled-old', value=Decimal(value), source_url=INDEX))
            session.add(MacroSeriesState(series_id=sid, catalog_version='2026-09-30-v7',
                active_source='nbs', latest_observed_at=date(2026, 7, 1), latest_released_at=released,
                last_success_at=OLD, last_checked_at=OLD, stale=False, consecutive_failures=0))
        session.add(MacroSeriesState(series_id=OTHER, catalog_version='2026-10-08-v9',
            active_source='fred', latest_observed_at=date(2026, 10, 8), last_checked_at=OLD,
            last_success_at=OLD, stale=False, consecutive_failures=0))
        session.add(JobRun(id='old-global-nbs', job_name='sync-macro', business_key='old-key',
            status='partial', started_at=OLD, finished_at=OLD, details={'asOf': OLD.isoformat(),
                'unresolvedGaps': {IDS[0]: [{'start': '2020-01-01', 'end': '2020-01-31'}]}}))
        session.flush()
        session.add(JobFailure(job_run_id='old-global-nbs', dataset=f'macro:{IDS[0]}',
            request_key='nbs', error_code='TIMEOUT', message='old evidence', retryable=True))
        session.add(JobCursor(job_name='backfill-macro-history', cursor_key='preserve', cursor_value={'done': 220}))
        for product in ('macro-insights', 'macro-transmission'):
            session.add(PublishedSnapshot(product=product, version='old-published', as_of=OLD,
                payload={'sentinel': product}, stale=True))


@pytest.fixture
def database(tmp_path):
    engine, factory = create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "nbs.sqlite"}')
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    seed(factory)
    yield Settings(database_url=str(engine.url)), factory, engine
    engine.dispose()


class ResponseSource:
    def __init__(self, kind='success'):
        self.kind, self.requests = kind, []
        self.pages = {}
        listing = []
        for title, url, body in ((CPI_TITLE, CPI_URL, CPI), (INDUSTRY_TITLE, INDUSTRY_URL, INDUSTRY)):
            listing.append(index(title, url))
            self.pages[url] = body
            older_url = url.replace('202609/', '202608/').replace('t202609', 't202608')
            older_title = title.replace('8月份', '7月份')
            listing.append(index(older_title, older_url))
            self.pages[older_url] = body.replace('8月份', '7月份').replace('2026/09/', '2026/08/')
        self.pages[INDEX] = ''.join(listing)

    def handle(self, request):
        assert request.method == 'GET' and request.url.host == 'www.stats.gov.cn'
        assert not request.url.query
        url = str(request.url)
        self.requests.append(url)
        if self.kind == '503': return httpx.Response(503, text='private-secret')
        if self.kind == 'empty': return httpx.Response(200, text=index('其他数据', CPI_URL))
        body = self.pages[url]
        if self.kind == 'malformed' and url != INDEX: body = body.replace('PubDate', 'OtherDate')
        return httpx.Response(200, text=body)


def execute(settings, source, *, mutate=None):
    with module().open_nbs_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        runtime.job._clock = lambda: NOW
        options = MacroSyncOptions(NOW, settings.macro_history_years,
            runtime.catalog.catalog_version, runtime.catalog.catalog_hash,
            force=True, mode='live', series_ids=IDS, publish=False)
        return runtime.job.run(mutate(options) if mutate else options)


def preserved(factory):
    with factory() as session:
        out = {}
        for model in (JobCursor, PublishedSnapshot):
            out[model.__name__] = [tuple(getattr(r, c.name) for c in model.__table__.columns)
                for r in session.scalars(select(model))]
        for model in (JobRun, JobFailure):
            out[model.__name__] = [tuple(getattr(r, c.name) for c in model.__table__.columns)
                for r in session.scalars(select(model))
                if (r.id if model is JobRun else r.job_run_id) == 'old-global-nbs']
        r = session.get(MacroSeriesState, OTHER)
        out['otherState'] = tuple(getattr(r, c.name) for c in MacroSeriesState.__table__.columns)
        out['oldCore'] = [tuple(getattr(r, c.name) for c in MacroObservation.__table__.columns)
            for r in session.scalars(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 7, 1)))]
        return out


def test_only_two_states_refresh_with_original_release_dates_and_no_publication(database):
    settings, factory, _ = database
    before = preserved(factory)
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.completed == 2
    assert report.request_count == 6 and report.row_count == 2 and report.publication is None
    with factory() as session:
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        assert all(r.catalog_version == '2026-10-08-v9' and not r.stale
                   and r.latest_observed_at == date(2026, 8, 1) for r in states)
        assert {r.latest_released_at.replace(tzinfo=UTC) for r in states} == {
            datetime(2026, 9, 9, 1, 30, tzinfo=UTC), datetime(2026, 9, 15, 2, tzinfo=UTC)}
        raws = session.scalars(select(SourcePayload)).all()
        assert len(raws) == 2
        assert {r.payload['request']['window']['start'] for r in raws} == {'2026-07-01'}
        assert session.scalar(select(JobRun).where(JobRun.job_name == 'sync-nbs')).request_count == 6
    assert preserved(factory) == before


def test_new_runtime_repeat_has_zero_new_core(database):
    settings, factory, _ = database
    assert execute(settings, ResponseSource()).row_count == 2
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 0 and report.request_count == 6
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 4


@pytest.mark.parametrize('kind', ['503', 'empty', 'malformed'])
def test_failed_or_empty_response_never_borrows_old_core_as_online_success(database, kind):
    settings, factory, _ = database
    before = preserved(factory)
    report = execute(settings, ResponseSource(kind))
    assert report.status == 'partial' and report.completed == report.row_count == 0
    assert report.request_count == (4 if kind == 'malformed' else 2)
    with factory() as session:
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        assert all(r.stale and r.last_success_at.replace(tzinfo=UTC) == OLD for r in states)
    assert preserved(factory) == before


def test_shared_budget_counts_index_and_articles_with_no_retry(database):
    settings, factory, _ = database
    report = execute(settings.model_copy(update={'macro_nbs_request_limit': 2}), ResponseSource())
    assert report.status == 'partial' and report.request_count == 2 and report.row_count == 0
    assert 'NBS_SYNC_REQUEST_BUDGET_EXHAUSTED' in {f['code'] for f in report.to_payload()['failures']}
    with factory() as session:
        assert 'NBS_SYNC_REQUEST_BUDGET_EXHAUSTED' in set(session.scalars(select(JobFailure.error_code)))


def test_configured_window_preserves_generic_overlap_and_catches_long_shutdown(database):
    settings, factory, _ = database
    settings = settings.model_copy(update={'macro_nbs_lookback_months': 1})
    with factory.begin() as session:
        for row in session.scalars(select(MacroObservation)):
            row.observed_at = date(2026, 6, 1)
    report = execute(settings, ResponseSource())
    assert report.status == 'partial' and report.completed == 0
    with factory() as session:
        raws = session.scalars(select(SourcePayload)).all()
        assert {r.payload['request']['window']['start'] for r in raws} == {'2026-06-01'}
        assert all(r.payload['data']['missingWindows'] == [{'start': '2026-06-01', 'end': '2026-06-30'}] for r in raws)
    assert MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).defaults.overlap.monthly_months == 18


@pytest.mark.parametrize('mutation', ['missing_core', 'future_state', 'wrong_scope', 'replay'])
def test_preflight_rejects_before_http_or_new_audit(database, mutation):
    settings, factory, _ = database
    if mutation == 'missing_core':
        with factory.begin() as session:
            for r in session.scalars(select(MacroObservation).where(MacroObservation.series_id == IDS[0])): session.delete(r)
    if mutation == 'future_state':
        with factory.begin() as session: session.get(MacroSeriesState, IDS[0]).last_checked_at = NOW + timedelta(days=1)
    changes = {'wrong_scope': {'series_ids': (IDS[0],)}, 'replay': {'mode': 'replay'}}
    source = ResponseSource()
    with pytest.raises(ValueError, match='NBS_SYNC_'):
        execute(settings, source, mutate=lambda o: replace(o, **changes.get(mutation, {})))
    assert not source.requests
    with factory() as session: assert session.scalars(select(JobRun).where(JobRun.job_name == 'sync-nbs')).all() == []


@pytest.mark.parametrize('change', [
    {'region': 'US'}, {'category': 'labor'}, {'unit': 'index'}, {'display_precision': 3},
    {'seasonal_adjustment': 'seasonally_adjusted'}, {'freshness_days': 365},
    {'source': {'params': {'maxIndexPages': '999'}}}, {'source': {'transform': 'yoy'}},
    {'source': {'source_url': 'https://example.com/'}}, {'source': {'series': '居民消费价格'}}])
def test_changed_metric_contract_is_rejected_before_http(database, monkeypatch, change):
    settings, factory, _ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.require(IDS[0])
    changed = series.model_copy(update={'sources': (series.sources[0].model_copy(update=change['source']),)}) if 'source' in change else series.model_copy(update=change)
    monkeypatch.setattr(module(), 'load_active_catalog', lambda: catalog.model_copy(update={
        'series': tuple(changed if r.id == IDS[0] else r for r in catalog.series)}))
    source = ResponseSource()
    with pytest.raises(ValueError, match='NBS_SYNC_SOURCE_INVALID'): execute(settings, source)
    assert not source.requests


def test_shared_lock_blocks_all_http(database):
    settings, factory, _ = database
    before = preserved(factory)
    lock = ProcessLocalJobLock()
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try: assert execute(settings, source).status == 'locked'
    finally: lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before


@pytest.mark.parametrize(('kind', 'exit_code'), [('success', 0), ('503', 4)])
def test_cli_real_runtime_and_safe_json_exit(database, monkeypatch, kind, exit_code):
    from app.cli import main as cli
    settings, factory, _ = database
    original = module().open_nbs_sync_runtime
    from contextlib import contextmanager
    @contextmanager
    def controlled_runtime(s):
        with original(s, transport=httpx.MockTransport(ResponseSource(kind).handle)) as runtime:
            # live任务以max(业务时间,执行时钟)判新鲜度，两处时钟均须受控。
            runtime.job._clock = lambda: NOW
            yield runtime
    monkeypatch.setattr(cli, 'open_nbs_sync_runtime', controlled_runtime, raising=False)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: settings)
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli, 'datetime', FixedTime)
    result = CliRunner().invoke(cli.app, ['sync-nbs'])
    assert result.exit_code == exit_code
    assert json.loads(result.stdout)['status'] == ('completed' if kind == 'success' else 'partial')
    assert 'private-secret' not in result.output and len(result.stdout.strip().splitlines()) == 1


def test_cli_hides_unknown_setup_exception(monkeypatch):
    from app.cli import main as cli
    def invalid(): raise ValueError('NBS_SYNC_CONFIG_INVALID private-secret')
    monkeypatch.setattr(cli.Settings, 'from_env', invalid)
    result = CliRunner().invoke(cli.app, ['sync-nbs'])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': 'NBS_SYNC_SETUP_UNAVAILABLE'}}


@pytest.mark.parametrize('values', [{'macro_nbs_request_limit': 1}, {'macro_nbs_request_limit': 101},
    {'macro_nbs_lookback_months': 0}, {'macro_nbs_lookback_months': 19}])
def test_invalid_root_configuration_is_rejected(values):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(**values)


def test_root_configuration_alias(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_NBS_REQUEST_LIMIT', '8')
    monkeypatch.setenv('DANG_MACRO_NBS_LOOKBACK_MONTHS', '2')
    settings = Settings.from_env()
    assert settings.macro_nbs_request_limit == 8 and settings.macro_nbs_lookback_months == 2
