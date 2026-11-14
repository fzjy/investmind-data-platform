"""预检必须读取真实计划/可见core/周历史；任何采集、状态或发布写入都应被发现。"""
import importlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.config import Settings
from app.db.base import ALL_BASES
from app.db.models import JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot
from app.db.session import create_engine_and_session
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog

NOW = datetime(2026, 10, 11, 2, tzinfo=UTC)
KEY = '2026-W41:2026-10-08-v9'


def module():
    assert importlib.util.find_spec('app.macro.jobs.macro_catchup_plan') is not None, 'missing read-only catchup preflight'
    return importlib.import_module('app.macro.jobs.macro_catchup_plan')


def seed(factory):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as s:
        for definition in catalog.series:
            if not definition.sources:
                continue
            period = date(2026, 10, 9) if definition.frequency == 'daily' else date(2026, 8, 1)
            provider = min(definition.sources, key=lambda source: source.priority).provider
            s.add(MacroObservation(source=provider, series_id=definition.id, name=definition.name,
                region=definition.region, frequency=definition.frequency, unit=definition.unit,
                seasonal_adjustment=definition.seasonal_adjustment, observed_at=period,
                released_at=NOW-timedelta(days=1), revision='controlled', value=Decimal('1'),
                source_url=str(definition.sources[0].source_url)))
            s.add(MacroSeriesState(series_id=definition.id, catalog_version=catalog.catalog_version,
                active_source=provider, latest_observed_at=period, latest_released_at=NOW-timedelta(days=1),
                last_checked_at=NOW-timedelta(days=1), last_success_at=NOW-timedelta(days=1),
                stale=False, consecutive_failures=0))
        for product in ('macro-insights', 'macro-transmission'):
            s.add(PublishedSnapshot(product=product, version='old-published', as_of=NOW-timedelta(days=1),
                payload={'sentinel':product}, stale=False))
        s.add(JobRun(id='manual-rebuild', job_name='sync-macro', business_key='rebuild-macro:controlled',
            status='completed', started_at=NOW-timedelta(days=1), details={
                'catalogVersion':catalog.catalog_version,'catalogHash':catalog.catalog_hash,
                'asOf':(NOW-timedelta(days=1)).isoformat(),'attempt':1,'unresolvedGaps':{}}))


def snapshot(factory):
    with factory() as s:
        return {table.fullname:[tuple(row) for row in s.execute(select(table).order_by(*table.primary_key.columns))]
            for base in ALL_BASES for table in base.metadata.sorted_tables}


def freeze_clock(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None): return NOW.astimezone(tz or UTC)
    monkeypatch.setattr(module(),'datetime',Frozen)


@pytest.fixture
def database(tmp_path):
    engine,factory = create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "catchup.sqlite"}')
    for base in ALL_BASES: base.metadata.create_all(engine)
    seed(factory)
    yield Settings(database_url=str(engine.url),fred_api_key='controlled-key',tushare_token='controlled-token'),factory,engine
    engine.dispose()


def test_preflight_is_read_only_and_manual_publish_is_not_weekly_completion(database):
    settings,factory,_ = database
    before = snapshot(factory)
    report = module().read_macro_catchup_plan(settings,now=NOW)
    assert report['readOnly'] and report['requestCount'] == 0
    assert report['weekly'] == {'enabled':True,'catchupEnabled':True,
        'latestDueAt':'2026-10-05T01:00:00+00:00','nextRunAt':'2026-10-12T01:00:00+00:00',
        'businessKey':KEY,'completed':False,'nextAttempt':1}
    assert report['activeCount'] == 18 and report['pausedSeries'] == ['cn.credit.tsf_stock_yoy','cn.fx.usdcny']
    assert report['decision'] == 'needs_bounded_startup' and not report['weeklyExecutionReady']
    assert report['publication']['versions'] == ['old-published']
    assert report['blockingReasons'] == ['WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED']
    assert snapshot(factory) == before
    assert 'controlled-key' not in json.dumps(report) and 'controlled-token' not in json.dumps(report)


@pytest.mark.parametrize('status,completed,next_attempt', [('completed',True,3),('partial',False,3),('running',False,3)])
def test_weekly_records_not_last_manual_run_determine_completion(database,status,completed,next_attempt):
    settings,factory,_ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as s:
        s.add(JobRun(id='week-run',job_name='sync-macro',business_key=KEY,status=status,
            details={'catalogVersion':catalog.catalog_version,'catalogHash':catalog.catalog_hash,'attempt':2}))
    before = snapshot(factory)
    report = module().read_macro_catchup_plan(settings,now=NOW)
    assert report['weekly']['completed'] is completed and report['weekly']['nextAttempt'] == next_attempt
    assert report['decision'] == ('already_completed' if completed else 'needs_bounded_startup')
    assert snapshot(factory) == before


@pytest.mark.parametrize('change,reason', [
    ('future','MACRO_STATE_FUTURE'),('missing_core','MACRO_CORE_NOT_VISIBLE'),
    ('invalidated','MACRO_CORE_NOT_VISIBLE'),('missing_state','MACRO_STATE_UNINITIALIZED'),
    ('old_catalog','MACRO_STATE_CATALOG_MISMATCH'),('gap','MACRO_ACTIVE_HISTORY_GAP'),
])
def test_preflight_surfaces_unsafe_evidence_without_clearing_it(database,change,reason):
    settings,factory,_ = database
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    sid='us.cpi.yoy'
    with factory.begin() as s:
        state=s.get(MacroSeriesState,sid)
        if change=='future': state.last_success_at=NOW+timedelta(days=1)
        if change=='missing_state': s.delete(state)
        if change=='old_catalog': state.catalog_version='old-catalog'
        if change in ('missing_core','invalidated'):
            row=s.scalar(select(MacroObservation).where(MacroObservation.series_id==sid))
            if change=='missing_core': s.delete(row)
            else: row.valid_until=NOW-timedelta(hours=1)
        if change=='gap':
            s.add(JobRun(id='gap',job_name='sync-macro',business_key='gap-key',status='partial',
                started_at=NOW,finished_at=NOW,details={'catalogVersion':catalog.catalog_version,
                    'catalogHash':catalog.catalog_hash,'unresolvedGaps':{sid:[{'start':'2020-01-01','end':'2020-02-01'}]}}))
    before=snapshot(factory)
    report=module().read_macro_catchup_plan(settings,now=NOW)
    assert reason in report['blockingReasons'] and report['decision']=='blocked'
    row=next(r for r in report['series'] if r['seriesId']==sid)
    assert reason in row['blockingReasons']
    assert snapshot(factory)==before


def test_group_windows_and_budget_are_not_misrepresented_as_global_cap(database):
    settings,_,_=database
    settings=settings.model_copy(update={'macro_nbs_lookback_months':2,'macro_nbs_request_limit':7,
        'macro_us_rates_request_limit':2,'macro_tushare_request_limit':8,'macro_fred_request_limit':20})
    report=module().read_macro_catchup_plan(settings,now=NOW)
    groups={g['command']:g for g in report['groups']}
    assert {k:v['httpLimit'] for k,v in groups.items()} == {
        'sync-us-rates':2,'sync-nbs':7,'sync-tushare-macro':8,'sync-fred-macro':20,
        'sync-govbond':None,'sync-dr007':None}
    assert report['totalHttpLimit'] is None
    nbs=next(r for r in report['series'] if r['seriesId']=='cn.cpi.core_yoy')
    assert nbs['genericWindow']['start']=='2025-04-01'
    assert nbs['scopedWindow']['start']=='2026-08-01'
    assert nbs['scopedWindow']['end']=='2026-10-11'


@pytest.mark.parametrize('setting', ['macro_sync_enabled','macro_sync_catchup_on_start'])
def test_disabled_schedule_does_not_propose_startup_execution(database,setting):
    settings,_,_=database
    report=module().read_macro_catchup_plan(settings.model_copy(update={setting:False}),now=NOW)
    assert report['decision']=='disabled' and not report['weeklyExecutionReady']


def test_cli_uses_real_reader_and_does_not_create_week_run(database,monkeypatch):
    from app.cli import main
    settings,factory,_=database
    before=snapshot(factory)
    monkeypatch.setattr(main.Settings,'from_env',lambda:settings)
    freeze_clock(monkeypatch)
    result=CliRunner().invoke(main.app,['plan-macro-catchup'])
    assert result.exit_code==0, result.output
    assert json.loads(result.stdout)['readOnly'] and json.loads(result.stdout)['activeCount']==18
    assert json.loads(result.stdout)['weekly']['businessKey']==KEY
    assert json.loads(result.stdout)['weekly']['latestDueAt']=='2026-10-05T01:00:00+00:00'
    assert len(result.stdout.strip().splitlines())==1 and snapshot(factory)==before


def test_cli_does_not_expose_config_failure_secrets(monkeypatch):
    from app.cli import main
    def fail(): raise RuntimeError('private-config-token-and-database-url')
    monkeypatch.setattr(main.Settings,'from_env',fail)
    result=CliRunner().invoke(main.app,['plan-macro-catchup'])
    assert result.exit_code==3
    assert json.loads(result.stdout)['error']['code']=='MACRO_CATCHUP_PLAN_UNAVAILABLE'
    assert 'private-config' not in result.output


def test_nonexistent_sqlite_is_not_created(tmp_path):
    target=tmp_path/'must-not-create.sqlite'
    with pytest.raises(ValueError): module().read_macro_catchup_plan(Settings(database_url=f'sqlite:///{target}'),now=NOW)
    assert not target.exists()


def test_catalog_hash_mismatch_is_not_accepted_or_repaired(database):
    settings,factory,_=database
    with factory.begin() as s: s.get(JobRun,'manual-rebuild').details={
        'catalogVersion':'2026-10-08-v9','catalogHash':'unproven-hash'}
    before=snapshot(factory)
    from app.macro.jobs.macro_sync_history import MacroCatalogVersionHashMismatch
    with pytest.raises(MacroCatalogVersionHashMismatch): module().read_macro_catchup_plan(settings,now=NOW)
    assert snapshot(factory)==before


@pytest.mark.parametrize('filename',['catchup#prod.sqlite','literal%23.sqlite','有空 格.sqlite'])
def test_sqlite_uri_preserves_exact_special_filename_without_creating_another(tmp_path,filename):
    target=tmp_path/filename
    engine,factory=create_engine_and_session(f'sqlite+pysqlite:///{target}')
    try:
        for base in ALL_BASES: base.metadata.create_all(engine)
        seed(factory)
        before=snapshot(factory)
        files=set(tmp_path.iterdir())
        try:
            report=module().read_macro_catchup_plan(Settings(database_url=str(engine.url),
                fred_api_key='controlled-key',tushare_token='controlled-token'),now=NOW)
        finally: assert set(tmp_path.iterdir())==files, 'preflight created a wrong-path database'
        assert report['activeCount']==18 and snapshot(factory)==before
    finally: engine.dispose()


def test_missing_state_scoped_window_still_uses_visible_core(database):
    settings,factory,_=database
    with factory.begin() as s: s.delete(s.get(MacroSeriesState,'us.cpi.yoy'))
    report=module().read_macro_catchup_plan(settings,now=NOW)
    row=next(r for r in report['series'] if r['seriesId']=='us.cpi.yoy')
    assert row['genericWindow']['start']=='2016-10-11'
    assert row['scopedWindow']['start']=='2025-04-01'
    assert 'MACRO_STATE_UNINITIALIZED' in row['blockingReasons']


def test_sqlite_plan_uses_one_read_snapshot_during_external_update(database,monkeypatch):
    from sqlalchemy import text
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    settings,factory,engine=database
    with engine.connect() as c: c.execute(text('PRAGMA journal_mode=WAL')).scalar()
    original=SqlMacroSyncHistory.inspect
    def interleaved(self,*args):
        result=original(self,*args)
        with factory.begin() as s:
            s.get(MacroSeriesState,'us.cpi.yoy').last_success_at=NOW+timedelta(days=1)
        return result
    monkeypatch.setattr(SqlMacroSyncHistory,'inspect',interleaved)
    report=module().read_macro_catchup_plan(settings,now=NOW)
    row=next(r for r in report['series'] if r['seriesId']=='us.cpi.yoy')
    assert 'MACRO_STATE_FUTURE' not in row['blockingReasons']
    with factory() as s: assert s.get(MacroSeriesState,'us.cpi.yoy').last_success_at.replace(tzinfo=UTC)==NOW+timedelta(days=1)


def test_reader_never_constructs_http_clients_or_executes_job(database,monkeypatch):
    import httpx
    from app.connectors.tushare.client import TushareClient
    from app.macro.jobs.macro_sync import MacroSyncJob
    settings,factory,_=database
    before=snapshot(factory)
    def forbidden(*args,**kwargs): pytest.fail('preflight attempted client construction or job execution')
    monkeypatch.setattr(httpx.Client,'__init__',forbidden)
    monkeypatch.setattr(TushareClient,'__init__',forbidden)
    monkeypatch.setattr(MacroSyncJob,'run',forbidden)
    report=module().read_macro_catchup_plan(settings,now=NOW)
    assert report['readOnly'] and snapshot(factory)==before


def test_sqlite_rejects_accidental_writer_in_preflight(database,monkeypatch):
    from sqlalchemy.exc import DBAPIError
    settings,factory,_=database
    before=snapshot(factory)
    def write(_settings,_now,read_factory):
        with read_factory.begin() as s:
            s.add(JobRun(id='must-not-write',job_name='sync-macro',business_key='forbidden',status='completed'))
            s.flush()
    monkeypatch.setattr(module(),'_read_plan',write)
    with pytest.raises(DBAPIError): module().read_macro_catchup_plan(settings,now=NOW)
    assert snapshot(factory)==before


@pytest.mark.parametrize('stamp,zone,weekday,local_time,due,next_run,key',[
    ('2026-10-12T00:59:59+00:00','Asia/Shanghai','mon',time(9),
        '2026-10-05T01:00:00+00:00','2026-10-12T01:00:00+00:00',KEY),
    ('2026-10-12T01:00:00+00:00','Asia/Shanghai','mon',time(9),
        '2026-10-12T01:00:00+00:00','2026-10-19T01:00:00+00:00','2026-W42:2026-10-08-v9'),
    ('2026-10-11T16:00:00+00:00','Asia/Shanghai','mon',time(9),
        '2026-10-05T01:00:00+00:00','2026-10-12T01:00:00+00:00',KEY),
    ('2026-10-12T00:00:00+08:00','Asia/Shanghai','mon',time(9),
        '2026-10-05T01:00:00+00:00','2026-10-12T01:00:00+00:00',KEY),
    ('2027-01-04T00:59:59+00:00','Asia/Shanghai','mon',time(9),
        '2026-12-28T01:00:00+00:00','2027-01-04T01:00:00+00:00','2026-W53:2026-10-08-v9'),
    ('2027-01-04T01:00:00+00:00','Asia/Shanghai','mon',time(9),
        '2027-01-04T01:00:00+00:00','2027-01-11T01:00:00+00:00','2027-W01:2026-10-08-v9'),
    ('2026-03-08T07:29:00+00:00','America/New_York','sun',time(2,30),
        '2026-03-01T07:30:00+00:00','2026-03-08T07:30:00+00:00','2026-W09:2026-10-08-v9'),
    ('2026-11-01T06:30:00+00:00','America/New_York','sun',time(1,30),
        '2026-11-01T05:30:00+00:00','2026-11-08T06:30:00+00:00','2026-W44:2026-10-08-v9'),
])
def test_preflight_integrates_timezone_due_and_iso_week_boundaries(database,stamp,zone,weekday,local_time,due,next_run,key):
    settings,_,_=database
    report=module().read_macro_catchup_plan(settings.model_copy(update={'sync_timezone':zone,
        'macro_sync_day_of_week':weekday,'macro_sync_time':local_time}),now=datetime.fromisoformat(stamp))
    assert report['weekly']['latestDueAt']==due and report['weekly']['nextRunAt']==next_run
    assert report['weekly']['businessKey']==key
