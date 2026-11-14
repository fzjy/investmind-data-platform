"""HTTP替身之外保留真实采集Job、数据库审计、状态和恢复读路径。"""
import importlib
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.connectors.macro.contracts import ProviderFetchResult, ProviderObservation, ProviderTransientError
from app.connectors.macro.registry import MacroProviderRegistry
from app.db.base import ALL_BASES
from app.db.models import JobRun, MacroObservation, PublishedSnapshot
from app.db.session import create_engine_and_session
from app.jobs.locks import ProcessLocalJobLock
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from backend.tests.jobs.test_macro_catchup_plan import seed, snapshot

NOW=datetime(2026,10,11,12,tzinfo=UTC)
KEY='2026-W41:2026-10-08-v9'


def module():
    assert importlib.util.find_spec('app.macro.jobs.macro_catchup') is not None, 'missing bounded weekly catchup'
    return importlib.import_module('app.macro.jobs.macro_catchup')


@pytest.fixture
def database(tmp_path):
    engine,factory=create_engine_and_session(f'sqlite+pysqlite:///{tmp_path / "catchup.sqlite"}')
    for base in ALL_BASES: base.metadata.create_all(engine)
    seed(factory)
    yield Settings(database_url=str(engine.url),fred_api_key='controlled-key',tushare_token='controlled-token'),factory,engine
    engine.dispose()


def controlled_groups(settings,factory,engine,monkeypatch):
    mod=module()
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    groups=[]
    for original in mod.macro_catchup_groups():
        def opener(_settings,*,transport,name=original.name):
            @contextmanager
            def opened():
                with httpx.Client(transport=transport,follow_redirects=False) as client:
                    class ControlledProvider:
                        def __init__(self,pid): self.provider_id=pid
                        def fetch(self,series,source,window):
                            try:
                                response=client.get('https://controlled.test/'+series.id)
                                if response.status_code!=200: raise ProviderTransientError('TIMEOUT',request_count=1)
                            except ProviderTransientError as exc:
                                return ProviderFetchResult(self.provider_id,'failed',(),{},exc.request_count,exc)
                            period=date(2026,10,9) if series.frequency=='daily' else date(2026,8,1)
                            return ProviderFetchResult(self.provider_id,'success',(
                                ProviderObservation(self.provider_id,series.id,period,NOW-timedelta(days=1),'new-controlled',Decimal('1.25'),str(source.source_url)),),
                                {'controlled':'response'},1)
                    ids={s.provider for definition in catalog.series for s in definition.sources}
                    registry=MacroProviderRegistry(tuple(ControlledProvider(pid) for pid in ids))
                    job=_build_macro_job(factory,registry,engine,catalog,_settings.sync_timezone,
                        history=SqlMacroSyncHistory(factory,job_name=name))
                    job._clock=lambda:NOW
                    yield MacroSyncRuntime(job,catalog)
            return opened()
        groups.append(mod.MacroCatchupGroup(original.name,original.series_ids,opener))
    monkeypatch.setattr(mod,'macro_catchup_groups',lambda:tuple(groups))


def execute(database,monkeypatch,*,limit=100,fail=None,now=NOW):
    settings,factory,engine=database
    controlled_groups(settings,factory,engine,monkeypatch)
    settings=settings.model_copy(update={'macro_catchup_request_limit':limit})
    calls=[]
    def handle(request):
        calls.append(request.url.path[1:])
        return httpx.Response(503 if request.url.path[1:]==fail else 200)
    report=module().run_macro_catchup(settings,transport=httpx.MockTransport(handle),now=now)
    return report,calls


def test_full_active_scope_is_partial_for_paused_sources_and_never_publishes(database,monkeypatch):
    report,calls=execute(database,monkeypatch)
    assert report['status']=='partial' and report['activeCoverageCompleted']
    assert report['businessKey']==KEY and report['completedGroups']==6 and report['requestCount']==18
    assert len(calls)==18 and report['publicationVersion'] is None
    with database[1]() as s:
        row=s.get(JobRun,report['jobRunId'])
        assert row.status=='partial' and row.job_name=='catchup-macro'
        assert row.details['activeCoverageCompleted']
        assert {p.version for p in s.scalars(select(PublishedSnapshot))}=={'old-published'}
        assert s.get(JobRun,'manual-rebuild').status=='completed'


def test_independent_reentry_skips_only_proven_current_scope(database,monkeypatch):
    first,_=execute(database,monkeypatch)
    before=snapshot(database[1])
    second,calls=execute(database,monkeypatch)
    assert first['activeCoverageCompleted'] and second['status']=='skipped'
    assert second['requestCount']==0 and calls==[] and snapshot(database[1])==before


def test_budget_stop_resumes_completed_groups_without_refetch(database,monkeypatch):
    first,calls=execute(database,monkeypatch,limit=2)
    assert first['requestCount']==2 and len(calls)==2 and not first['activeCoverageCompleted']
    assert first['completedGroups']==1 and first['budgetExhausted']
    second,calls=execute(database,monkeypatch)
    assert second['activeCoverageCompleted'] and second['requestCount']==16
    assert 'us.rate.effective_fed_funds' not in calls and 'us.rate.treasury_10y' not in calls


def test_partial_group_is_retried_and_not_checkpointed_as_complete(database,monkeypatch):
    first,_=execute(database,monkeypatch,fail='cn.cpi.core_yoy')
    assert not first['activeCoverageCompleted']
    second,calls=execute(database,monkeypatch)
    assert second['activeCoverageCompleted'] and set(calls)=={'cn.cpi.core_yoy','cn.industry.value_added_yoy'}


def test_checkpoint_does_not_trust_running_or_corrupt_group_reference(database,monkeypatch):
    first,_=execute(database,monkeypatch)
    with database[1].begin() as s:
        row=s.get(JobRun,first['jobRunId'])
        values=dict(row.details)
        group=dict(values['groups']['sync-us-rates'])
        s.get(JobRun,group['jobRunId']).status='running'
        values['groups']={**values['groups'],'sync-us-rates':group}
        row.details=values
    second,calls=execute(database,monkeypatch)
    assert second['activeCoverageCompleted'] and set(calls)=={'us.rate.effective_fed_funds','us.rate.treasury_10y'}


def test_next_due_week_requires_new_actual_requests(database,monkeypatch):
    execute(database,monkeypatch)
    second,calls=execute(database,monkeypatch,now=datetime(2026,10,12,1,tzinfo=UTC))
    assert second['businessKey']=='2026-W42:2026-10-08-v9' and len(calls)==18


def test_checkpoint_rejects_success_reference_from_another_week(database,monkeypatch):
    first,_=execute(database,monkeypatch)
    with database[1].begin() as session:
        parent=session.get(JobRun,first['jobRunId'])
        group_id=parent.details['groups']['sync-us-rates']['jobRunId']
        session.get(JobRun,group_id).business_key='2026-W40:2026-10-08-v9'
    second,calls=execute(database,monkeypatch)
    assert second['activeCoverageCompleted'] and len(calls)==2
    assert set(calls)=={'us.rate.effective_fed_funds','us.rate.treasury_10y'}


def test_before_monday_schedule_resumes_previous_due_week_without_refetch(database,monkeypatch):
    before_due=datetime(2026,10,12,0,tzinfo=UTC)
    first,calls=execute(database,monkeypatch,now=before_due)
    assert first['businessKey']==KEY and first['activeCoverageCompleted'] and len(calls)==18
    second,calls=execute(database,monkeypatch,now=before_due+timedelta(minutes=1))
    assert second['status']=='skipped' and calls==[]


@pytest.mark.parametrize('field',['macro_sync_enabled','macro_sync_catchup_on_start'])
def test_disabled_has_zero_http_and_zero_writes(database,monkeypatch,field):
    settings,factory,engine=database
    before=snapshot(factory)
    result=module().run_macro_catchup(settings.model_copy(update={field:False}),
        transport=httpx.MockTransport(lambda r:pytest.fail('disabled sent HTTP')),now=NOW)
    assert result['status']=='disabled' and result['requestCount']==0 and snapshot(factory)==before


def test_week_lock_conflict_does_not_create_audit_or_send_http(database,monkeypatch):
    settings,factory,engine=database
    controlled_groups(settings,factory,engine,monkeypatch)
    lock=ProcessLocalJobLock()
    assert lock.try_acquire('catchup-macro:'+KEY)
    before=snapshot(factory)
    try:
        result=module().run_macro_catchup(settings,transport=httpx.MockTransport(lambda r:pytest.fail('locked sent HTTP')),now=NOW)
    finally: lock.release('catchup-macro:'+KEY)
    assert result['status']=='locked' and snapshot(factory)==before


def test_unsafe_preflight_is_blocked_without_mutation(database,monkeypatch):
    settings,factory,engine=database
    with factory.begin() as s:
        row=s.scalar(select(MacroObservation).where(MacroObservation.series_id=='cn.cpi.yoy'))
        s.delete(row)
    before=snapshot(factory)
    result=module().run_macro_catchup(settings,transport=httpx.MockTransport(lambda r:pytest.fail('unsafe sent HTTP')),now=NOW)
    assert result['status']=='blocked' and 'MACRO_CORE_NOT_VISIBLE' in result['blockingReasons']
    assert snapshot(factory)==before


def test_naive_time_is_refused_without_creating_a_week_record(database):
    before=snapshot(database[1])
    with pytest.raises(ValueError): module().run_macro_catchup(database[0],now=datetime(2026,10,11))
    assert snapshot(database[1])==before


def test_proven_checkpoint_still_requires_lock_held_runtime_contract(database,monkeypatch):
    execute(database,monkeypatch)
    before=snapshot(database[1])
    from app.macro.jobs.macro_sync import MacroSyncJob
    assert callable(getattr(MacroSyncJob,'preflight',None)), 'missing audit-free runtime contract validation'
    def reject(self,options): raise ValueError('changed-history-manifest')
    monkeypatch.setattr(MacroSyncJob,'preflight',reject)
    report,calls=execute(database,monkeypatch)
    assert report['status']=='blocked' and calls==[] and snapshot(database[1])==before


def test_runtime_contract_preflight_does_not_write_or_send_http(database,monkeypatch):
    from app.macro.jobs.macro_sync import MacroSyncJob
    assert callable(getattr(MacroSyncJob,'preflight',None)), 'missing audit-free runtime contract validation'
    settings,factory,engine=database
    controlled_groups(settings,factory,engine,monkeypatch)
    group=module().macro_catchup_groups()[0]
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    from app.jobs.locks import data_publication_lock_key
    options=MacroSyncOptions(NOW,settings.macro_history_years,catalog.catalog_version,catalog.catalog_hash,
        force=True,mode='live',series_ids=group.series_ids,publish=False)
    before=snapshot(factory)
    lock=ProcessLocalJobLock()
    seen=[]
    with group.opener(settings,transport=httpx.MockTransport(lambda r:pytest.fail('preflight sent HTTP'))) as runtime:
        def verify(opts):
            assert not lock.try_acquire(data_publication_lock_key()), 'contract validated outside publication lock'
            seen.append(opts.series_ids)
        runtime.job._before_work=verify
        assert runtime.job.preflight(options)
    assert seen==[('us.rate.effective_fed_funds','us.rate.treasury_10y')] and snapshot(factory)==before


def test_process_interruption_preserves_committed_group_and_next_attempt(database,monkeypatch):
    mod=module()
    original=mod._save_progress
    def crash(*args):
        original(*args)
        raise KeyboardInterrupt('controlled shutdown after committed checkpoint')
    monkeypatch.setattr(mod,'_save_progress',crash)
    with pytest.raises(KeyboardInterrupt): execute(database,monkeypatch)
    with database[1]() as s:
        interrupted=s.scalar(select(JobRun).where(JobRun.job_name=='catchup-macro'))
        old_id=interrupted.id
        assert interrupted.status=='running' and interrupted.details['groups']['sync-us-rates']['status']=='completed'
    monkeypatch.setattr(mod,'_save_progress',original)
    second,calls=execute(database,monkeypatch)
    assert second['attempt']==2 and second['requestCount']==16 and second['activeCoverageCompleted']
    with database[1]() as s: assert s.get(JobRun,old_id).status=='running'


def test_changed_planning_config_invalidates_group_checkpoints(database,monkeypatch):
    execute(database,monkeypatch)
    settings,factory,engine=database
    controlled_groups(settings,factory,engine,monkeypatch)
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200)
    result=module().run_macro_catchup(settings.model_copy(update={'macro_nbs_lookback_months':2}),
        transport=httpx.MockTransport(handle),now=NOW)
    assert result['activeCoverageCompleted'] and len(calls)==18


def test_read_only_plan_reports_bounded_checkpoint_not_generic_weekly_completion(database,monkeypatch):
    execute(database,monkeypatch)
    from app.macro.jobs.macro_catchup_plan import read_macro_catchup_plan
    before=snapshot(database[1])
    report=read_macro_catchup_plan(database[0],now=NOW)
    assert report.get('boundedCatchup',{}).get('activeScopeCompleted') is True, 'missing bounded catchup evidence'
    assert report['boundedCatchup']['httpLimit']==100 and report['boundedCatchup']['completedGroups']==6
    assert report['boundedCatchup']['runtimePreflightStillRequired'] is True
    assert report['weekly']['completed'] is False and snapshot(database[1])==before
