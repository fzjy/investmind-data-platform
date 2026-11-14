"""FRED 六项实际 runtime/CLI/存储测试，只替换外部 HTTP 边界。"""
from dataclasses import replace
from contextlib import contextmanager
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


IDS = ('us.gdp.real_qoq_annualized', 'us.cpi.yoy', 'us.cpi.core_yoy', 'us.pce.core_yoy',
       'us.labor.unemployment_rate', 'us.labor.nonfarm_payroll_change')
FRED_IDS = ('A191RL1Q225SBEA', 'CPIAUCSL', 'CPILFESL', 'PCEPILFE', 'UNRATE', 'PAYEMS')
VALUES = ('2.2', '5', '5', '5', '4.1', '30')
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
OLD = datetime(2026, 9, 1, tzinfo=UTC)
KEY = 'controlled-private-fred-key'
OTHER = 'cn.cpi.core_yoy'


def module():
    assert importlib.util.find_spec('app.macro.jobs.fred_macro_sync_runtime') is not None, 'missing scoped FRED macro runtime'
    return importlib.import_module('app.macro.jobs.fred_macro_sync_runtime')


def seed(factory):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for sid, value in zip(IDS, VALUES):
            s = catalog.require(sid)
            period = date(2026, 4, 1) if s.frequency == 'quarterly' else date(2026, 7, 1)
            session.add(MacroObservation(source='fred', series_id=sid, name=s.name, region=s.region,
                frequency=s.frequency, unit=s.unit, seasonal_adjustment=s.seasonal_adjustment,
                observed_at=period, released_at=OLD, revision='controlled-old', value=Decimal(value),
                source_url=str(s.sources[0].source_url)))
            session.add(MacroSeriesState(series_id=sid, catalog_version='2026-09-30-v7', active_source='fred',
                latest_observed_at=period, latest_released_at=OLD, last_checked_at=OLD,
                last_success_at=OLD, stale=False, consecutive_failures=0))
        session.add(MacroSeriesState(series_id=OTHER, catalog_version='2026-10-08-v9',
            active_source='nbs', last_checked_at=OLD, last_success_at=OLD, stale=False, consecutive_failures=0))
        session.add(JobRun(id='old-global-fred', job_name='sync-macro', business_key='old-key', status='partial',
            started_at=OLD, finished_at=OLD, details={'asOf': OLD.isoformat(),
                'unresolvedGaps': {IDS[0]: [{'start': '2020-01-01', 'end': '2020-03-31'}]}}))
        session.flush()
        session.add(JobFailure(job_run_id='old-global-fred', dataset=f'macro:{IDS[0]}',
            request_key='fred', error_code='TIMEOUT', message='old evidence', retryable=True))
        session.add(JobCursor(job_name='old-history', cursor_key='preserve', cursor_value={'next': 4}))
        for product in ('macro-insights', 'macro-transmission'):
            session.add(PublishedSnapshot(product=product, version='old-published', as_of=OLD,
                payload={'sentinel': product}, stale=True))


@pytest.fixture
def database(tmp_path):
    engine, factory = create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "fred-macro.sqlite"}')
    for base in (Base, ExternalBase, MartBase): base.metadata.create_all(engine)
    seed(factory)
    yield Settings(database_url=str(engine.url), fred_api_key=KEY), factory, engine
    engine.dispose()


def level(period, value, start='2026-09-01', end='2026-10-11'):
    return {'date': period, 'value': value, 'realtime_start': start, 'realtime_end': end}


class ResponseSource:
    def __init__(self, kind='success'):
        self.kind, self.requests = kind, []

    def handle(self, request):
        if request.method == 'POST':
            assert str(request.url) == 'https://api.bls.gov/publicAPI/v1/timeseries/data/'
            q = json.loads(request.content)
            assert q['seriesid'][0] in ('CUSR0000SA0', 'CUSR0000SA0L1E', 'LNS14000000', 'CES0000000001')
            assert q['startyear'] in ('2024', '2025') and q['endyear'] == '2026'
            self.requests.append({'provider': 'bls', **q})
            if self.kind != 'fallback': return httpx.Response(503, json={'error': KEY})
            assert q['seriesid'] == ['CUSR0000SA0']
            return httpx.Response(200, json={'status': 'REQUEST_SUCCEEDED', 'message': KEY, 'Results': {'series': [{
                'seriesID': 'CUSR0000SA0', 'data': [{'year': '2025', 'period': 'M09', 'value': '100'},
                    {'year': '2026', 'period': 'M09', 'value': '108'}]}]}})
        assert request.method == 'GET' and request.url.host == 'api.stlouisfed.org'
        q = dict(request.url.params)
        assert q['api_key'] == KEY and q['series_id'] in FRED_IDS
        self.requests.append({'provider': 'fred', 'path': request.url.path, **{k:v for k,v in q.items() if k != 'api_key'}})
        if self.kind in ('503', '429', '401'): return httpx.Response(int(self.kind), json={'error': KEY})
        if request.url.path.endswith('/vintagedates'):
            dates = [] if self.kind == 'empty' else ['2026-09-01']
            if self.kind == 'revision': dates.append('2026-09-30')
            return httpx.Response(200, json={'realtime_start': q['realtime_start'], 'realtime_end': q['realtime_end'],
                'sort_order': 'asc', 'count': 100 if self.kind == 'paging' else len(dates),
                'offset': int(q['offset']), 'limit': 10000, 'vintage_dates': dates})
        assert request.url.path == '/fred/series/observations' and q['units'] == 'lin'
        sid = q['series_id']
        assert q['observation_start'] == ('2024-01-01' if sid == FRED_IDS[0] else
            '2024-04-01' if sid in FRED_IDS[1:4] else '2025-03-01' if sid == 'PAYEMS' else '2025-04-01')
        assert q['observation_end'] == q['realtime_end'] == '2026-10-11' and q['realtime_start'] == '2026-09-01'
        if self.kind == 'fallback' and sid == 'CPIAUCSL': return httpx.Response(503, json={'error': KEY})
        period = '2026-05-01' if self.kind == 'expired' else '2026-08-01'
        if sid == FRED_IDS[0]: rows = [level('2026-01-01' if self.kind == 'expired' else '2026-04-01', '2.2')]
        elif sid == 'UNRATE': rows = [level(period, '4.1')]
        elif sid == 'PAYEMS': rows = [level('2026-04-01' if self.kind == 'expired' else '2026-07-01', '120'), level(period, '150')]
        else: rows = [level('2025-05-01' if self.kind == 'expired' else '2025-08-01', '100'), level(period, '105')]
        if self.kind == 'malformed': rows[-1]['value'] = KEY
        if self.kind == 'revision' and sid in ('CPIAUCSL', 'PAYEMS'):
            rows[0]['realtime_end'] = '2026-09-29'
            rows.insert(1, level(rows[0]['date'], '125', start='2026-09-30'))
        return httpx.Response(200, json={'realtime_start': '2026-09-01', 'realtime_end': '2026-10-11',
            'observation_start': q['observation_start'], 'observation_end': q['observation_end'],
            'units': 'lin', 'output_type': 1, 'file_type': 'json', 'order_by': 'observation_date',
            'sort_order': 'asc', 'count': len(rows), 'offset': int(q['offset']), 'limit': 100000, 'observations': rows})


def execute(settings, source, *, mutate=None):
    with module().open_fred_macro_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        runtime.job._clock = lambda: NOW
        options = MacroSyncOptions(NOW, settings.macro_history_years, runtime.catalog.catalog_version,
            runtime.catalog.catalog_hash, force=True, mode='live', series_ids=IDS, publish=False)
        return runtime.job.run(mutate(options) if mutate else options)


@pytest.mark.parametrize('instant,expected_day', [
    (datetime(2026, 10, 11, 2, tzinfo=UTC), '2026-10-10'),
    (datetime(2026, 10, 11, 12, tzinfo=UTC), '2026-10-11'),
    (datetime(2026, 1, 11, 5, 30, tzinfo=UTC), '2026-01-10'),
])
def test_fred_source_calendar_boundary(database, instant, expected_day):
    settings, factory, _ = database
    seen = []

    def handle(request):
        q = dict(request.url.params)
        seen.append(q)
        assert q['realtime_end'] == expected_day
        if request.url.path.endswith('/vintagedates'):
            return httpx.Response(200, json={'count': 1, 'offset': 0, 'vintage_dates': [expected_day]})
        assert q['observation_end'] == expected_day
        period = '2025-12-01' if instant.month == 1 else '2026-08-01'
        base = '2024-12-01' if instant.month == 1 else '2025-08-01'
        return httpx.Response(200, json={'observations': [level(base, '100', start=expected_day, end=expected_day),
            level(period, '105', start=expected_day, end=expected_day)]})

    from app.connectors.macro.contracts import MacroFetchWindow
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    definition = catalog.require(IDS[1])
    # 直接测试专用适配器，不更改 job 的真实检查时刻或已有 generic provider。
    from app.connectors.macro.transport import MacroHttpTransport
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        http = MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,))
        from app.connectors.macro.fred import CalendarFredMacroProvider
        provider = CalendarFredMacroProvider(KEY, transport=http)
        result = provider.fetch(definition, definition.sources[0],
            MacroFetchWindow(date(2025, 1, 1), instant.date(), instant))
    assert result.status == 'success' and result.request_count == len(seen) == 2
    assert len(result.observations) == 1 and result.observations[0].value == Decimal('5')
    assert result.observations[0].valid_until is None  # 请求截断不是修订失效证据。
    assert result.raw_payload['sourceCalendar']['end'] == expected_day
    assert result.raw_payload['sourceCalendar']['timezone'] == 'America/Chicago'


def preserved(factory):
    with factory() as session:
        out = {}
        for model in (JobCursor, PublishedSnapshot, JobRun, JobFailure, MacroSeriesState, MacroObservation):
            rows = session.scalars(select(model)).all()
            if model is JobRun: rows = [r for r in rows if r.id == 'old-global-fred']
            if model is JobFailure: rows = [r for r in rows if r.job_run_id == 'old-global-fred']
            if model is MacroSeriesState: rows = [r for r in rows if r.series_id == OTHER]
            if model is MacroObservation: rows = [r for r in rows if r.revision == 'controlled-old']
            out[model.__name__] = [tuple(getattr(r,c.name) for c in model.__table__.columns) for r in rows]
        return out


def test_six_real_primary_calls_transform_and_refresh_only_target_states(database):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource()
    report = execute(settings, source)
    assert report.status == 'completed' and report.completed == 6 and report.request_count == 12
    assert report.row_count == 6 and report.publication is None and len(source.requests) == 12
    with factory() as session:
        rows = session.scalars(select(MacroObservation).where(MacroObservation.revision == '2026-09-01')).all()
        assert {r.series_id:r.value for r in rows} == dict(zip(IDS, map(Decimal, VALUES)))
        assert all(r.catalog_version == '2026-10-08-v9' and not r.stale for r in
            session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))))
        assert len(session.scalars(select(SourcePayload)).all()) == 6
    assert preserved(factory) == before


def test_repeat_keeps_vintage_time_and_inserts_no_core(database):
    settings, factory, _ = database
    assert execute(settings, ResponseSource()).row_count == 6
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 0 and report.request_count == 12
    with factory() as session: assert len(session.scalars(select(MacroObservation)).all()) == 12


def test_base_revision_closes_original_interval_without_changing_value_and_adds_new_vintage(database):
    settings, factory, _ = database
    execute(settings, ResponseSource())
    report = execute(settings, ResponseSource('revision'))
    assert report.status == 'completed' and report.row_count == 2
    with factory() as session:
        for sid, original, revised in ((IDS[1], Decimal('5'), Decimal('-16')), (IDS[5], Decimal('30'), Decimal('25'))):
            rows = {r.revision:r for r in session.scalars(select(MacroObservation).where(
                MacroObservation.series_id == sid, MacroObservation.observed_at == date(2026,8,1)))}
            assert rows['2026-09-01'].value == original
            assert rows['2026-09-01'].valid_until.replace(tzinfo=UTC) == datetime(2026,9,30,tzinfo=UTC)
            assert rows['2026-09-30'].value == revised and rows['2026-09-30'].valid_until is None


def test_registered_bls_fallback_is_used_but_does_not_hide_primary_failure(database):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource('fallback')
    report = execute(settings, source)
    assert report.status == 'partial' and report.completed == 5 and report.request_count == 13
    outcome = next(o for o in report.outcomes if o.series_id == IDS[1])
    assert outcome.actual_source == 'bls' and outcome.fallback_used and outcome.status == 'stale'
    with factory() as session:
        state = session.get(MacroSeriesState, IDS[1])
        assert state.last_success_at.replace(tzinfo=UTC) == NOW and state.fallback_used
        assert any(failure.provider == "fred" for failure in outcome.source_failures)
        assert session.scalar(select(MacroObservation).where(MacroObservation.source == 'bls')).value == Decimal('8')
        assert KEY not in json.dumps([r.payload for r in session.scalars(select(SourcePayload))])
    assert preserved(factory) == before


@pytest.mark.parametrize('kind', ['503', '429', '401', 'empty', 'expired', 'malformed'])
def test_failures_never_retry_or_borrow_old_core_as_success_or_leak_keys(database, kind):
    settings, factory, _ = database
    before = preserved(factory)
    source = ResponseSource(kind)
    report = execute(settings, source)
    assert report.status == 'partial' and report.completed == 0 and report.request_count == len(source.requests)
    assert report.request_count == (10 if kind in ('503','429','401','empty') else 12 if kind == 'expired' else 16)
    with factory() as session:
        expected_success = NOW if kind == "expired" else OLD
        assert all(r.stale == (kind != "expired") and r.last_success_at.replace(tzinfo=UTC) == expected_success for r in
            session.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(IDS))))
        assert KEY not in json.dumps([r.payload for r in session.scalars(select(SourcePayload))])
    assert KEY not in json.dumps(report.to_payload()) and preserved(factory) == before


def test_vintage_paging_and_bls_share_budget_and_blocked_calls_count_zero(database):
    settings, factory, _ = database
    settings = settings.model_copy(update={'macro_fred_request_limit':12})
    source = ResponseSource('paging')
    report = execute(settings, source)
    assert report.status == 'partial' and report.request_count == len(source.requests) == 12
    assert report.outcomes[-1].request_count == 0 and report.row_count == 0
    assert 'FRED_MACRO_REQUEST_BUDGET_EXHAUSTED' in {f['code'] for f in report.to_payload()['failures']}
    with factory() as session: assert sum(r.payload['requestCount'] for r in session.scalars(select(SourcePayload))) == 12


@pytest.mark.parametrize('mutation', ['missing_core','future_state','wrong_scope','replay','history','no_force'])
def test_preflight_rejects_before_http_or_new_collection_audit(database, mutation):
    settings, factory, _ = database
    if mutation == 'missing_core':
        with factory.begin() as session:
            for r in session.scalars(select(MacroObservation).where(MacroObservation.series_id == IDS[0])): session.delete(r)
    if mutation == 'future_state':
        with factory.begin() as session: session.get(MacroSeriesState,IDS[0]).last_checked_at = NOW+timedelta(days=1)
    changes = {'wrong_scope': {'series_ids':(IDS[0],)}, 'replay':{'mode':'replay'},
               'history':{'history_years':1}, 'no_force':{'force':False}}
    source = ResponseSource()
    with pytest.raises(ValueError, match='FRED_MACRO_'):
        execute(settings, source, mutate=lambda o:replace(o,**changes.get(mutation,{})))
    assert not source.requests
    with factory() as session: assert session.scalars(select(JobRun).where(JobRun.job_name == 'sync-fred-macro')).all() == []


@pytest.mark.parametrize('change', [{'unit':'index'},{'category':'labor'},{'frequency':'daily'},
    {'seasonal_adjustment':'not_adjusted'},{'freshness_days':365},{'display_precision':0},
    {'source':{'series':'GDP'}},{'source':{'transform':'pc1'}},{'source':{'params':{'units':'pc1'}}},
    {'source':{'display_allowed':False}},{'source':{'source_url':'https://example.com/'}},
    {'fallback':{'series':'LNS14000000'}},{'fallback':{'source_url':'https://example.com/'}},
    {'remove_fallback':True}])
def test_complete_primary_metric_and_registered_fallback_contracts_are_checked(database, monkeypatch, change):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    sid = IDS[1] if 'fallback' in change or 'remove_fallback' in change else IDS[0]
    s = catalog.require(sid)
    if 'source' in change: altered = s.model_copy(update={'sources':(s.sources[0].model_copy(update=change['source']),)})
    elif 'fallback' in change: altered = s.model_copy(update={'sources':(s.sources[0],s.sources[1].model_copy(update=change['fallback']))})
    elif 'remove_fallback' in change: altered = s.model_copy(update={'sources':(s.sources[0],)})
    else: altered = s.model_copy(update=change)
    monkeypatch.setattr(module(),'load_active_catalog',lambda:catalog.model_copy(update={
        'series':tuple(altered if r.id == sid else r for r in catalog.series)}))
    source = ResponseSource()
    with pytest.raises(ValueError, match='FRED_MACRO_SOURCE_INVALID'): execute(database[0], source)
    assert not source.requests


def test_shared_publication_lock_blocks_all_http(database):
    settings, factory, _ = database
    before = preserved(factory)
    lock = ProcessLocalJobLock()
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try: assert execute(settings,source).status == 'locked'
    finally: lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before


@pytest.mark.parametrize(('kind','code'), [('success',0),('503',4)])
def test_cli_real_runtime_returns_one_safe_json(database, monkeypatch, kind, code):
    from app.cli import main as cli
    original = module().open_fred_macro_sync_runtime
    @contextmanager
    def fixed_runtime(settings):
        with original(settings, transport=httpx.MockTransport(ResponseSource(kind).handle)) as runtime:
            # CLI及内部live planner使用同一受控时刻，不随实际执行日期漂移。
            runtime.job._clock = lambda: NOW
            yield runtime
    monkeypatch.setattr(cli, 'open_fred_macro_sync_runtime', fixed_runtime, raising=False)
    monkeypatch.setattr(cli.Settings,'from_env',lambda:database[0])
    class FixedTime(datetime):
        @classmethod
        def now(cls,tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli,'datetime',FixedTime)
    result = CliRunner().invoke(cli.app,['sync-fred-macro'])
    assert result.exit_code == code, result.stdout
    assert json.loads(result.stdout)['status'] == ('completed' if code == 0 else 'partial')
    assert KEY not in result.output and len(result.stdout.strip().splitlines()) == 1


def test_cli_setup_error_prefix_with_secret_is_never_exposed(monkeypatch):
    from app.cli import main as cli
    def invalid(): raise ValueError('FRED_MACRO_CONFIG_INVALID '+KEY)
    monkeypatch.setattr(cli.Settings,'from_env',invalid)
    result = CliRunner().invoke(cli.app,['sync-fred-macro'])
    assert result.exit_code == 3 and json.loads(result.stdout) == {
        'status':'failed','error':{'code':'FRED_MACRO_SYNC_SETUP_UNAVAILABLE'}}


@pytest.mark.parametrize('limit',[0,11,101])
def test_root_budget_validates_minimum_six_vintage_observation_pairs(limit):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(macro_fred_request_limit=limit)


def test_budget_reads_root_configuration_alias(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_FRED_REQUEST_LIMIT','18')
    assert Settings.from_env().macro_fred_request_limit == 18
