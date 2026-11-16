"""仅替换 HTTP；实际 CLI/runtime/raw/core/state 验证两项利率的隔离更新。"""
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
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


IDS = ('us.rate.effective_fed_funds', 'us.rate.treasury_10y')
# 原请求合同是美国/UTC同日；跨日由共享日历专项单独验证。
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=13)


def module():
    assert importlib.util.find_spec('app.macro.jobs.us_rates_sync_runtime') is not None, 'missing scoped US rates runtime'
    return importlib.import_module('app.macro.jobs.us_rates_sync_runtime')


def seed(factory):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for sid, value in zip(IDS, ('3.88', '5.24')):
            series = catalog.require(sid)
            session.add(MacroObservation(source='fred', series_id=sid, name=series.name,
                region=series.region, frequency=series.frequency, unit=series.unit,
                seasonal_adjustment=series.seasonal_adjustment, observed_at=date(2026, 9, 28),
                released_at=OLD, revision='old-controlled', value=Decimal(value),
                source_url=str(series.sources[0].source_url)))
            session.add(MacroSeriesState(series_id=sid, catalog_version='2026-09-30-v7',
                active_source='fred', latest_observed_at=date(2026, 9, 28), latest_released_at=OLD,
                last_success_at=OLD, last_checked_at=OLD, stale=False, consecutive_failures=0))
        session.add(MacroSeriesState(series_id='cn.cpi.core_yoy', catalog_version='2026-09-30-v7',
            active_source='nbs', latest_observed_at=date(2026, 8, 1), last_checked_at=OLD,
            last_success_at=OLD, stale=False, consecutive_failures=0))
        session.add(JobRun(id='old-global', job_name='sync-macro', business_key='old-key', status='partial',
            started_at=OLD, finished_at=OLD, details={'asOf': OLD.isoformat(),
                'unresolvedGaps': {IDS[0]: [{'start': '2020-01-01', 'end': '2020-01-02'}]}}))
        session.flush()
        session.add(JobFailure(job_run_id='old-global', dataset=f'macro:{IDS[0]}',
            request_key='fred', error_code='TIMEOUT', message='old evidence', retryable=True))
        session.add(JobCursor(job_name='old-history', cursor_key='preserve', cursor_value={'next': 4}))
        for product in ('macro-insights', 'macro-transmission'):
            session.add(PublishedSnapshot(product=product, version='old-published', as_of=OLD,
                payload={'sentinel': product}, stale=True))


@pytest.fixture
def database(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    seed(factory)
    settings = Settings(database_url=engine.url.render_as_string(hide_password=False), fred_api_key='controlled-fred-key')
    yield settings, factory, engine
    engine.dispose()


class ResponseSource:
    def __init__(self, kind='success'):
        self.kind, self.requests = kind, []

    def handle(self, request):
        assert request.method == 'GET' and request.url.host == 'api.stlouisfed.org'
        assert request.url.path == '/fred/series/observations'
        q = dict(request.url.params)
        assert q['api_key'] == 'controlled-fred-key'
        assert q['series_id'] in ('DFF', 'DGS10') and q['units'] == 'lin'
        assert q['observation_end'] == '2026-10-11'
        assert q['realtime_start'] == q['realtime_end'] == '2026-10-11'
        self.requests.append({k: v for k, v in q.items() if k != 'api_key'})
        if self.kind in ('503', '429', '401'):
            return httpx.Response(int(self.kind), json={'error': 'controlled-fred-key'})
        old = '3.88' if q['series_id'] == 'DFF' else '5.24'
        rows = [{'date': '2026-09-28', 'value': old, 'realtime_start': '2026-10-11', 'realtime_end': '9999-12-31'},
                {'date': '2026-10-09', 'value': '3.80' if q['series_id'] == 'DFF' else '5.00',
                 'realtime_start': '2026-10-11', 'realtime_end': '9999-12-31'}]
        if self.kind == 'empty': rows = []
        if self.kind == 'expired': rows = rows[:1]
        if self.kind == 'paging': rows = [rows[0]]
        if self.kind == 'malformed': rows = [{**rows[-1], 'value': 'not-number'}]
        return httpx.Response(200, json={'realtime_start': '2026-10-11', 'realtime_end': '2026-10-11',
            'observation_start': q['observation_start'], 'observation_end': q['observation_end'],
            'units': 'lin', 'output_type': 1, 'file_type': 'json', 'order_by': 'observation_date',
            'sort_order': 'asc', 'count': 3 if self.kind == 'paging' else len(rows),
            'offset': int(q['offset']), 'limit': 100000, 'observations': rows})


def execute(settings, source, *, mutate=None):
    with module().open_us_rates_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        runtime.job._clock = lambda: NOW
        options = MacroSyncOptions(NOW, settings.macro_history_years,
            runtime.catalog.catalog_version, runtime.catalog.catalog_hash,
            force=True, mode='live', series_ids=IDS, publish=False)
        if mutate: options = mutate(options)
        return runtime.job.run(options)


def preserved(factory):
    with factory() as session:
        result = {}
        for model in (JobCursor, JobFailure, PublishedSnapshot):
            result[model.__name__] = [tuple(getattr(r, c.name) for c in model.__table__.columns)
                for r in session.scalars(select(model)) if model is not JobFailure or r.job_run_id == 'old-global']
        r = session.get(JobRun, 'old-global')
        result['oldRun'] = tuple(getattr(r, c.name) for c in JobRun.__table__.columns)
        r = session.get(MacroSeriesState, 'cn.cpi.core_yoy')
        result['otherState'] = tuple(getattr(r, c.name) for c in MacroSeriesState.__table__.columns)
        result['oldCore'] = [tuple(getattr(r, c.name) for c in MacroObservation.__table__.columns)
            for r in session.scalars(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 28)))]
        return result


def test_scoped_success_records_only_two_states_without_global_skip_or_publication(database):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource()
    report = execute(settings, source)
    assert report.status == 'completed' and report.planned == report.completed == 2
    assert report.request_count == report.row_count == 2 and report.publication is None
    assert {q['observation_start'] for q in source.requests} == {'2026-09-11'}
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 4
        assert len(session.scalars(select(SourcePayload)).all()) == 2
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        assert all(r.catalog_version == '2026-10-08-v9' and not r.stale
                   and r.latest_observed_at == date(2026, 10, 9) for r in states)
        runs = session.scalars(select(JobRun).where(JobRun.job_name == 'sync-us-rates')).all()
        assert len(runs) == 1 and runs[0].request_count == 2
        assert 'controlled-fred-key' not in json.dumps([r.payload for r in session.scalars(select(SourcePayload))])
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    assert SqlMacroSyncHistory(factory).inspect(macro_sync_business_key(NOW, catalog.catalog_version,
        'Asia/Shanghai'), catalog.catalog_version, catalog.catalog_hash) == (False, 1)
    assert preserved(factory) == before


def test_new_runtime_repeat_is_idempotent(database):
    settings, factory, _ = database
    assert execute(settings, ResponseSource()).row_count == 2
    repeat = execute(settings, ResponseSource())
    assert repeat.status == 'completed' and repeat.row_count == 0 and repeat.request_count == 2
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 4
        assert session.scalar(select(PublishedSnapshot)).version == 'old-published'


@pytest.mark.parametrize('kind', ['503', '429', '401', 'empty', 'expired', 'malformed'])
def test_failure_never_retries_falls_back_or_borrows_old_core_as_success(database, kind):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource(kind)
    report = execute(settings, source)
    assert report.status == 'partial' and report.completed == 0 and report.request_count == 2
    assert len(source.requests) == 2 and report.row_count == 0
    with factory() as session:
        states = session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))).all()
        expected_success = NOW if kind == "expired" else OLD
        assert all(r.stale == (kind != "expired") and r.last_success_at.replace(tzinfo=UTC) == expected_success for r in states)
    assert preserved(factory) == before


def test_pagination_cannot_exceed_shared_request_budget(database):
    settings, factory, _ = database
    settings = settings.model_copy(update={'macro_us_rates_request_limit': 2})
    source = ResponseSource('paging')
    report = execute(settings, source)
    assert report.status == 'partial' and report.request_count == 2 and report.row_count == 0
    assert len(source.requests) == 2
    assert 'US_RATES_REQUEST_BUDGET_EXHAUSTED' in {f['code'] for f in report.to_payload()['failures']}
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 2
        assert 'US_RATES_REQUEST_BUDGET_EXHAUSTED' in set(session.scalars(select(JobFailure.error_code)))


@pytest.mark.parametrize('mutation', ['missing_core', 'future_state', 'wrong_scope', 'wrong_source'])
def test_preflight_rejects_before_network_and_new_job_audit(database, monkeypatch, mutation):
    from dataclasses import replace
    settings, factory, _ = database
    if mutation == 'missing_core':
        with factory.begin() as session:
            for r in session.scalars(select(MacroObservation).where(MacroObservation.series_id == IDS[0])):
                session.delete(r)
    if mutation == 'future_state':
        with factory.begin() as session:
            session.get(MacroSeriesState, IDS[0]).last_checked_at = NOW + timedelta(days=1)
    if mutation == 'wrong_source':
        catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        series = catalog.require(IDS[0])
        changed = series.model_copy(update={'sources': (series.sources[0].model_copy(update={'series': 'SOFR'}), *series.sources[1:])})
        catalog = catalog.model_copy(update={'series': tuple(changed if r.id == IDS[0] else r for r in catalog.series)})
        monkeypatch.setattr(module(), 'load_active_catalog', lambda: catalog)
    source = ResponseSource()
    with pytest.raises(ValueError, match='US_RATES_'):
        execute(settings, source, mutate=(lambda o: replace(o, series_ids=(IDS[0],))) if mutation == 'wrong_scope' else None)
    assert not source.requests
    with factory() as session:
        assert session.scalars(select(JobRun).where(JobRun.job_name == 'sync-us-rates')).all() == []


@pytest.mark.parametrize('change', [
    {'seasonal_adjustment': 'seasonally_adjusted'}, {'freshness_days': 365},
    {'region': 'CN'}, {'category': 'labor'}, {'display_precision': 6},
    {'primary': {'api': 'vintagedates'}}, {'primary': {'params': {'units': 'pc1'}}},
    {'primary': {'source_url': 'https://example.com/other'}},
    {'primary': {'response_fields': ('date', 'other')}}, {'extra_fred': True},
])
def test_changed_metric_contract_and_extra_fred_are_rejected_in_preflight(database, monkeypatch, change):
    settings, factory, _ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.require(IDS[0])
    if 'primary' in change:
        from app.macro.catalog import MacroSourceDefinition
        primary = MacroSourceDefinition.model_validate({**series.sources[0].model_dump(), **change['primary']})
        sources = (primary, *series.sources[1:])
        changed = series.model_copy(update={'sources': sources})
    elif 'extra_fred' in change:
        sources = (*series.sources[:1], series.sources[1].model_copy(update={'provider': 'fred', 'series': 'SOFR'}))
        changed = series.model_copy(update={'sources': sources})
    else:
        changed = series.model_copy(update=change)
    catalog = catalog.model_copy(update={'series': tuple(changed if r.id == IDS[0] else r for r in catalog.series)})
    monkeypatch.setattr(module(), 'load_active_catalog', lambda: catalog)
    before = preserved(factory)
    source = ResponseSource('503')
    with pytest.raises(ValueError, match='US_RATES_SOURCE_INVALID'):
        execute(settings, source)
    assert source.requests == [] and preserved(factory) == before
    with factory() as session:
        assert session.scalars(select(JobRun).where(JobRun.job_name == 'sync-us-rates')).all() == []


def test_dedicated_execution_catalog_has_only_approved_primary_sources(database):
    settings, _, _ = database
    with module().open_us_rates_sync_runtime(settings) as runtime:
        assert all(len(runtime.job._catalog.require(sid).sources) == 1 for sid in IDS)
        assert len(runtime.catalog.require(IDS[0]).sources) == 2


def test_shared_publication_lock_stops_before_network(database):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource()
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert execute(settings, source).status == 'locked'
    finally:
        lock.release(data_publication_lock_key())
    assert source.requests == [] and preserved(factory) == before


def test_cli_uses_real_scoped_runtime_and_maps_partial_exit(database, monkeypatch):
    from app.cli import main as cli
    settings, factory, _ = database
    m = module()
    source = ResponseSource('empty')
    original = m.open_us_rates_sync_runtime
    monkeypatch.setattr(cli, 'open_us_rates_sync_runtime', lambda s: original(s, transport=httpx.MockTransport(source.handle)), raising=False)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: settings)
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli, 'datetime', FixedTime)
    result = CliRunner().invoke(cli.app, ['sync-us-rates'])
    assert result.exit_code == 4 and json.loads(result.stdout)['status'] == 'partial'
    assert len(result.stdout.strip().splitlines()) == 1
    assert 'controlled-fred-key' not in result.output
    with factory() as session:
        assert session.scalar(select(JobRun).where(JobRun.job_name == 'sync-us-rates')).status == 'partial'


def test_cli_setup_failure_hides_unknown_exception_text(monkeypatch):
    from app.cli import main as cli
    def invalid(): raise ValueError('US_RATES_CONFIG_INVALID db-password controlled-fred-key')
    monkeypatch.setattr(cli.Settings, 'from_env', invalid)
    result = CliRunner().invoke(cli.app, ['sync-us-rates'])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': 'US_RATES_SYNC_SETUP_UNAVAILABLE'}}


@pytest.mark.parametrize('limit', [0, 1, 101])
def test_budget_configuration_rejects_invalid_values(limit):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(macro_us_rates_request_limit=limit)


def test_budget_configuration_loads_from_root_env_alias(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_US_RATES_REQUEST_LIMIT', '3')
    assert Settings.from_env().macro_us_rates_request_limit == 3
