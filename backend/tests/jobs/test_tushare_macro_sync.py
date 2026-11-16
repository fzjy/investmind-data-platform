"""限定六项宏观增量：真实 CLI/runtime/数据库，仅替换 HTTP 边界。"""
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
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot, SourcePayload
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


IDS = ('cn.gdp.real_yoy', 'cn.cpi.yoy', 'cn.ppi.yoy', 'cn.pmi.manufacturing', 'cn.money.m1_yoy', 'cn.money.m2_yoy')
VALUES = ('4.7', '0.8', '3.8', '50.1', '4.1', '7.5')
NOW = datetime(2026, 10, 11, 1, 20, tzinfo=UTC)
OLD = datetime(2026, 9, 30, 10, tzinfo=UTC)
TOKEN = 'controlled-tushare-private-key'
OTHER = 'cn.cpi.core_yoy'


def module():
    assert importlib.util.find_spec('app.macro.jobs.tushare_macro_sync_runtime') is not None, 'missing scoped Tushare macro runtime'
    return importlib.import_module('app.macro.jobs.tushare_macro_sync_runtime')


def seed(factory):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for sid, value in zip(IDS, VALUES):
            s = catalog.require(sid)
            period = date(2026, 4, 1) if s.frequency == 'quarterly' else date(2026, 7, 1)
            session.add(MacroObservation(source='tushare', series_id=sid, name=s.name,
                region=s.region, frequency=s.frequency, unit=s.unit, seasonal_adjustment=s.seasonal_adjustment,
                observed_at=period, released_at=OLD, revision='controlled-old', value=Decimal(value),
                source_url=str(s.sources[0].source_url)))
            session.add(MacroSeriesState(series_id=sid, catalog_version='2026-09-30-v7',
                active_source='tushare', latest_observed_at=period, latest_released_at=OLD,
                last_success_at=OLD, last_checked_at=OLD, stale=False, consecutive_failures=0))
        session.add(MacroSeriesState(series_id=OTHER, catalog_version='2026-10-08-v9',
            active_source='nbs', last_success_at=OLD, last_checked_at=OLD, stale=False, consecutive_failures=0))
        session.add(JobRun(id='old-global-ts', job_name='sync-macro', business_key='old-key',
            status='partial', started_at=OLD, finished_at=OLD, details={'asOf': OLD.isoformat(),
                'unresolvedGaps': {IDS[0]: [{'start': '2020-01-01', 'end': '2020-03-31'}]}}))
        session.flush()
        session.add(JobFailure(job_run_id='old-global-ts', dataset=f'macro:{IDS[0]}',
            request_key='tushare', error_code='TIMEOUT', message='old evidence', retryable=True))
        session.add(JobCursor(job_name='old-history', cursor_key='preserve', cursor_value={'next': 4}))
        for product in ('macro-insights', 'macro-transmission'):
            session.add(PublishedSnapshot(product=product, version='old-published', as_of=OLD,
                payload={'sentinel': product}, stale=True))


@pytest.fixture
def database(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    seed(factory)
    settings = Settings(database_url=engine.url.render_as_string(hide_password=False), tushare_token=TOKEN,
        tushare_standard_requests_per_minute=600000)
    yield settings, factory, engine
    engine.dispose()


class ResponseSource:
    def __init__(self, kind='success'):
        self.kind, self.requests = kind, []

    def handle(self, request):
        assert request.method == 'POST' and str(request.url) == 'https://api.tushare.pro'
        q = json.loads(request.content)
        assert q['token'] == TOKEN
        self.requests.append({k: v for k, v in q.items() if k != 'token'})
        if self.kind in ('503', '429', '401'): return httpx.Response(int(self.kind), json={'msg': TOKEN})
        if self.kind == 'permission': return httpx.Response(200, json={'code': -1, 'msg': '权限不足 ' + TOKEN})
        contracts = {'cn_gdp': {'gdp_yoy': '4.7'}, 'cn_cpi': {'nt_yoy': '0.8'},
            'cn_ppi': {'ppi_yoy': '3.8'}, 'cn_pmi': {'pmi010000': '50.1'},
            'cn_m': {'m1_yoy': '4.1', 'm2_yoy': '7.5'}}
        assert q['api_name'] in contracts and q['fields'].split(',')[1] in contracts[q['api_name']]
        quarterly = q['api_name'] == 'cn_gdp'
        assert q['params'] == ({'start_q': '2024Q1', 'end_q': '2026Q4'} if quarterly
            else {'start_m': '202504', 'end_m': '202610'}) or q['params'] == {'start_q': '2024Q1', 'end_q': '2024Q1'}
        period = '2026Q2' if quarterly else '202608'
        if self.kind == 'expired': period = '2026Q1' if quarterly else '202607'
        value = contracts[q['api_name']][q['fields'].split(',')[1]]
        if self.kind == 'malformed': value = TOKEN
        rows = [] if self.kind == 'empty' else [[period, value]]
        return httpx.Response(200, json={'code': 0, 'msg': '', 'data': {'fields': q['fields'].split(','), 'items': rows}})


def execute(settings, source, *, mutate=None):
    with module().open_tushare_macro_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
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
                for r in session.scalars(select(model)) if (r.id if model is JobRun else r.job_run_id) == 'old-global-ts']
        r = session.get(MacroSeriesState, OTHER)
        out['otherState'] = tuple(getattr(r, c.name) for c in MacroSeriesState.__table__.columns)
        out['oldCore'] = [tuple(getattr(r, c.name) for c in MacroObservation.__table__.columns)
            for r in session.scalars(select(MacroObservation).where(MacroObservation.revision == 'controlled-old'))]
        return out


def test_six_real_calls_only_refresh_six_states_without_global_skip_or_publication(database):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource()
    report = execute(settings, source)
    assert report.status == 'completed' and report.completed == 6
    assert report.request_count == 6 and report.row_count == 5 and report.publication is None
    assert len(source.requests) == 6
    with factory() as session:
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        assert all(r.catalog_version == '2026-10-08-v9' and not r.stale for r in states)
        assert session.get(MacroSeriesState, IDS[0]).latest_released_at.replace(tzinfo=UTC) == OLD
        assert all(r.latest_released_at.replace(tzinfo=UTC) == NOW for r in states if r.series_id != IDS[0])
        assert len(session.scalars(select(SourcePayload)).all()) == 6
    assert preserved(factory) == before


def test_repeat_preserves_original_detection_time_and_adds_no_core(database):
    settings, factory, _ = database
    assert execute(settings, ResponseSource()).row_count == 5
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 0 and report.request_count == 6
    with factory() as session: assert len(session.scalars(select(MacroObservation)).all()) == 11


@pytest.mark.parametrize('kind', ['503', '429', '401', 'permission', 'empty', 'expired', 'malformed'])
def test_no_retry_or_offline_success_and_credentials_never_enter_raw(database, kind):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource(kind)
    report = execute(settings, source)
    assert report.status == 'partial' and report.completed == 0 and report.request_count == 6
    assert len(source.requests) == 6
    with factory() as session:
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        expected_success = NOW if kind == "expired" else OLD
        assert all(r.stale == (kind != "expired") and r.last_success_at.replace(tzinfo=UTC) == expected_success for r in states)
        assert TOKEN not in json.dumps([r.payload for r in session.scalars(select(SourcePayload))])
    assert TOKEN not in json.dumps(report.to_payload()) and preserved(factory) == before


def test_budget_includes_own_gap_calls_and_does_not_count_blocked_attempt_as_http(database):
    settings, factory, _ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        session.add(JobRun(id='old-scoped-gap', job_name='sync-tushare-macro', business_key='old-scoped',
            started_at=OLD, finished_at=OLD, status='partial', details={'asOf': OLD.isoformat(),
                'catalogVersion': catalog.catalog_version, 'catalogHash': catalog.catalog_hash,
                'unresolvedGaps': {IDS[0]: [{'start': '2024-01-01', 'end': '2024-03-31'}]}}))
    source = ResponseSource()
    report = execute(settings, source)
    assert report.status == 'partial' and len(source.requests) == report.request_count == 6
    assert 'TUSHARE_MACRO_REQUEST_BUDGET_EXHAUSTED' in {f['code'] for f in report.to_payload()['failures']}
    assert report.outcomes[-1].request_count == 0
    with factory() as session:
        assert sum(r.payload['requestCount'] for r in session.scalars(select(SourcePayload))) == 6


@pytest.mark.parametrize('mutation', ['missing_core', 'future_state', 'wrong_scope', 'replay', 'history'])
def test_preflight_rejects_before_network_and_new_audit(database, mutation):
    settings, factory, _ = database
    if mutation == 'missing_core':
        with factory.begin() as session:
            for r in session.scalars(select(MacroObservation).where(MacroObservation.series_id == IDS[0])): session.delete(r)
    if mutation == 'future_state':
        with factory.begin() as session: session.get(MacroSeriesState, IDS[0]).last_checked_at = NOW + timedelta(days=1)
    changes = {'wrong_scope': {'series_ids': (IDS[0],)}, 'replay': {'mode': 'replay'}, 'history': {'history_years': 1}}
    source = ResponseSource()
    with pytest.raises(ValueError, match='TUSHARE_MACRO_'):
        execute(settings, source, mutate=lambda o: replace(o, **changes.get(mutation, {})))
    assert not source.requests
    with factory() as session: assert session.scalars(select(JobRun).where(JobRun.job_name == 'sync-tushare-macro')).all() == []


@pytest.mark.parametrize('change', [{'unit': 'index'}, {'region': 'US'}, {'category': 'labor'},
    {'freshness_days': 365}, {'seasonal_adjustment': 'seasonally_adjusted'}, {'display_precision': 3},
    {'source': {'api': 'cn_cpi'}}, {'source': {'field': 'gdp'}}, {'source': {'params': {'offset': '0'}}},
    {'source': {'display_allowed': False}}, {'source': {'source_url': 'https://example.com/'}},
    {'source': {'response_fields': ('quarter', 'gdp')}}])
def test_metric_and_full_source_contract_changes_are_rejected(database, monkeypatch, change):
    settings, factory, _ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    s = catalog.require(IDS[0])
    changed = s.model_copy(update={'sources': (s.sources[0].model_copy(update=change['source']),)}) if 'source' in change else s.model_copy(update=change)
    monkeypatch.setattr(module(), 'load_active_catalog', lambda: catalog.model_copy(update={
        'series': tuple(changed if r.id == IDS[0] else r for r in catalog.series)}))
    source = ResponseSource()
    with pytest.raises(ValueError, match='TUSHARE_MACRO_SOURCE_INVALID'): execute(settings, source)
    assert not source.requests


def test_shared_publication_lock_blocks_http(database):
    settings, factory, _ = database
    before = preserved(factory)
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try: assert execute(settings, source).status == 'locked'
    finally: lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before


@pytest.mark.parametrize(('kind', 'exit_code'), [('success', 0), ('permission', 4)])
def test_cli_real_runtime_safe_json_and_exit(database, monkeypatch, kind, exit_code):
    from app.cli import main as cli
    settings, factory, _ = database
    original = module().open_tushare_macro_sync_runtime
    from contextlib import contextmanager
    @contextmanager
    def controlled_runtime(s):
        with original(s, transport=httpx.MockTransport(ResponseSource(kind).handle)) as runtime:
            # 不依赖日历推进导致历史样本自动过期；生产新鲜度判断不变。
            runtime.job._clock = lambda: NOW
            yield runtime
    monkeypatch.setattr(cli, 'open_tushare_macro_sync_runtime', controlled_runtime, raising=False)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: settings)
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli, 'datetime', FixedTime)
    result = CliRunner().invoke(cli.app, ['sync-tushare-macro'])
    assert result.exit_code == exit_code
    assert json.loads(result.stdout)['status'] == ('completed' if kind == 'success' else 'partial')
    assert TOKEN not in result.output and len(result.stdout.strip().splitlines()) == 1


def test_cli_unknown_setup_error_does_not_leak_secrets(monkeypatch):
    from app.cli import main as cli
    def invalid(): raise ValueError('TUSHARE_MACRO_CONFIG_INVALID ' + TOKEN)
    monkeypatch.setattr(cli.Settings, 'from_env', invalid)
    result = CliRunner().invoke(cli.app, ['sync-tushare-macro'])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': 'TUSHARE_MACRO_SYNC_SETUP_UNAVAILABLE'}}


@pytest.mark.parametrize('limit', [0, 5, 101])
def test_invalid_root_budget_is_rejected(limit):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(macro_tushare_request_limit=limit)


def test_budget_uses_root_alias(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_TUSHARE_REQUEST_LIMIT', '8')
    assert Settings.from_env().macro_tushare_request_limit == 8
