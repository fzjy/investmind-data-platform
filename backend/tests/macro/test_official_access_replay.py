"""已保存官方诊断原件的零HTTP正式恢复，不改原失败。"""
from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import JobRun,IngestionBatch,SourcePayload,MacroObservation
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_official_access_check import prepared,execute,CHECK,state
from backend.tests.macro.test_official_macro_store import plan,store
from backend.tests.macro.test_official_household_credit import document

REPLAY=CHECK+timedelta(seconds=10)


def module():
    try:return import_module('app.macro.official_access_replay')
    except ModuleNotFoundError:pytest.fail('official zero-HTTP replay not implemented')


def ready(factory,body=None):
    parent,c=prepared(factory)
    result=execute(factory,parent,c,transport=httpx.MockTransport(lambda _:response(200,
        content=document(title='2026年前八个月金融统计数据报告') if body is None else body)))
    return parent,c,result


def test_prepare_replay_and_reentry_use_real_raw_identity_without_new_http_or_parent_mutation(database):
    _,f=database;parent,c,diag=ready(f);before=state(f);m=module()
    recovery=m.OfficialAccessReplayStore(f,parent)
    assert recovery.inspect(REPLAY)['status']=='ready'
    key=recovery.prepare(c,REPLAY)
    assert key!=parent and key!=diag['probeRunId'] and count(f,MacroObservation)==0
    with f() as s:
        row=s.get(JobRun,key);assert row.request_count==0 and row.details['rawId']==s.scalar(select(SourcePayload)).id
        assert row.details['recovery']['parentRunId']==parent
    result=store(f).replay(key,REPLAY+timedelta(seconds=1))
    assert result['insertedRows']==1 and count(f,MacroObservation)==1 and count(f,SourcePayload)==1
    after=state(f)
    for table in ('job_run','ingestion_batch','source_payload','job_cursor'):
        for original in before[table]:
            assert next(r for r in after[table] if r['id']==original['id'])==original
    assert store(f).replay(key,REPLAY+timedelta(seconds=2))['supplierHttp']==0
    assert recovery.prepare(c,REPLAY+timedelta(seconds=2))==key and state(f)==after


def test_recovered_document_unlocks_only_original_remaining_slot_and_public_view_is_source_bound(database):
    _,f=database;parent,c,_=ready(f);m=module();key=m.OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    store(f).replay(key,REPLAY+timedelta(seconds=1))
    p=plan();current=store(f).inspect(p,REPLAY+timedelta(seconds=2))
    assert current['status']=='partial' and current['reservedCount']==1
    assert current['documents'][0]['runId']==key and current['documents'][0]['originalRunId']==parent
    with f() as s:assert s.get(JobRun,parent).status=='failed'
    second=store(f).reserve(p,p['documents'][1],REPLAY+timedelta(seconds=3))
    store(f).receive(second,document(amount='1.30'),REPLAY+timedelta(seconds=4))
    store(f).replay(second,REPLAY+timedelta(seconds=5))
    assert store(f).inspect(p,REPLAY+timedelta(seconds=6))['status']=='completed'
    with f() as s:assert sum(r.request_count for r in s.scalars(select(JobRun)))==3  # 原2普通＋独立1诊断，恢复0。
    view_module=import_module('app.macro.official_macro_view')
    view=view_module.load_official_macro_view(f,REPLAY+timedelta(seconds=7))
    assert view_module.official_macro_view_valid(view)
    g=view['groups']['householdCredit'];d=g['documents'][0]
    assert d['runId']==key and d['recovery']['additionalHttpCount']==1 and d['recovery']['replayHttpCount']==0
    assert g['series'][1]['latest']=={'month':'2026-09-01','value':'900'}


def test_late_recovery_never_becomes_visible_at_old_asof_and_clock_regression_cannot_write_core(database):
    _,f=database;parent,c,_=ready(f);m=module();key=m.OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    with pytest.raises(ValueError):store(f).replay(key,REPLAY-timedelta(seconds=1))
    assert count(f,MacroObservation)==0
    store(f).replay(key,REPLAY+timedelta(seconds=1))
    vm=import_module('app.macro.official_macro_view')
    old=vm.load_official_macro_view(f,REPLAY-timedelta(seconds=1))['groups']['householdCredit']
    assert old['documents']==[] and old['latestAttempt']['status']=='failed'
    with f() as s:assert s.scalar(select(MacroObservation)).released_at.replace(tzinfo=REPLAY.tzinfo)==REPLAY+timedelta(seconds=1)


@pytest.mark.parametrize('bad',['missing','invalid-body','raw','parent','off'])
def test_missing_invalid_or_changed_evidence_never_creates_replay(database,bad):
    _,f=database
    if bad=='missing':parent,c=prepared(f)
    else:parent,c,_=ready(f,document() if bad=='invalid-body' else None)
    if bad=='raw':
        with f.begin() as s:s.scalar(select(SourcePayload)).content_hash='bad'
    if bad=='parent':
        with f.begin() as s:s.get(JobRun,parent).request_count=2
    if bad=='off':
        from app.macro.extensions.config import ExtensionConfig
        v=c.model_dump();v['groups']['householdCredit']['collectionEnabled']=False;c=ExtensionConfig.model_validate(v)
    before=state(f)
    with pytest.raises(ValueError):module().OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    assert state(f)==before and count(f,MacroObservation)==0


def test_historical_read_remains_valid_after_current_policy_migrates(database):
    _,f=database;parent,c,_=ready(f);m=module();key=m.OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    store(f).replay(key,REPLAY+timedelta(seconds=1))
    assert m.OfficialAccessReplayStore(f,parent).inspect(REPLAY+timedelta(seconds=2))['status']=='completed'
    with f() as s:
        row=s.get(JobRun,key);store(f)._run(s,row)
    # 历史来源复核不读取正在运行的配置，prepare新写仍必须用当前合法配置。
    with f() as s:
        from app.macro.official_access_check import OfficialAccessCheckStore
        evidence=OfficialAccessCheckStore(f,parent).evidence(s,REPLAY+timedelta(seconds=2))
        assert evidence['result']['validation']=='passed'


@pytest.mark.parametrize('field',['parent','check','hash','time','budget','extra','identity'])
def test_public_recovery_proof_rejects_independently_rehashed_forgery(database,field):
    _,f=database;parent,c,_=ready(f);m=module();key=m.OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    store(f).replay(key,REPLAY+timedelta(seconds=1))
    vm=import_module('app.macro.official_macro_view');value=vm.load_official_macro_view(f,REPLAY+timedelta(seconds=2))
    item=value['groups']['householdCredit']['documents'][0];proof=item['recovery']
    if field=='parent':proof['parentRunId']='00000000-0000-5000-8000-000000000000'
    elif field=='check':proof['accessCheckRunId']='00000000-0000-5000-8000-000000000000'
    elif field=='hash':proof['accessCheckEvidenceHash']='bad'
    elif field=='time':proof['accessCheckCompletedAt']=(REPLAY+timedelta(days=1)).isoformat()
    elif field=='budget':proof['replayHttpCount']=1
    elif field=='extra':proof['extra']=True
    else:item['runId']=parent
    from app.macro.extensions.plan import digest
    value['evidenceHash']=digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not vm.official_macro_view_valid(value)


def test_completed_original_root_can_migrate_and_recovery_evidence_survives_in_history(database):
    _,f=database;parent,c,_=ready(f);key=module().OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    store(f).replay(key,REPLAY+timedelta(seconds=1));p=plan()
    second=store(f).reserve(p,p['documents'][1],REPLAY+timedelta(seconds=2))
    store(f).receive(second,document(amount='1.30'),REPLAY+timedelta(seconds=3));store(f).replay(second,REPLAY+timedelta(seconds=4))
    from backend.tests.macro.test_household_discovery import complete_graph
    later=REPLAY+timedelta(days=28);complete_graph(f,later,'1.40')
    vm=import_module('app.macro.official_macro_view');value=vm.load_official_macro_view(f,later+timedelta(seconds=10))
    assert vm.official_macro_view_valid(value)
    group=value['groups']['householdCredit']
    assert group['series'][1]['latest']=={'month':'2026-10-01','value':'1000'}
    assert len(group['history'])==1 and group['history'][0]['documents'][0]['recovery']['parentRunId']==parent
    with f() as s:assert s.get(JobRun,parent).status=='failed'


def test_replay_raw_and_core_binding_tamper_blocks_effective_root(database):
    _,f=database;parent,c,_=ready(f);m=module();key=m.OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    store(f).replay(key,REPLAY+timedelta(seconds=1))
    with f.begin() as s:
        row=s.get(JobRun,key);details=deepcopy(row.details);details['recovery']['accessCheckEvidenceHash']='0'*64;row.details=details
    with pytest.raises(ValueError):store(f).inspect(plan(),REPLAY+timedelta(seconds=2))


@pytest.mark.parametrize('field',['budget','raw-id'])
def test_replay_strict_identity_and_budget_never_accept_boolean_as_integer(database,field):
    _,f=database;parent,c,_=ready(f);key=module().OfficialAccessReplayStore(f,parent).prepare(c,REPLAY)
    with f.begin() as s:
        row=s.get(JobRun,key);details=deepcopy(row.details)
        if field=='budget':details['recovery']['additionalHttpCount']=True
        else:details['rawId']=True
        row.details=details
        flag_modified(row,'details')
    with f() as s:
        persisted=s.get(JobRun,key).details
        assert (persisted['recovery']['additionalHttpCount'] if field=='budget' else persisted['rawId']) is True
        with pytest.raises(ValueError):store(f)._run(s,s.get(JobRun,key))
