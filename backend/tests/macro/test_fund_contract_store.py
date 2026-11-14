from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import SourcePayload
from app.macro.extensions.plan import digest
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_fund_contract_discovery import NOW,plan,body


def store(factory):return import_module('app.macro.fund_contract_store').FundContractStore(factory)


def test_raw_first_replay_and_completed_root_stay_fixed_next_day(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path)
    assert s.inspect(p,NOW)['phase']=='new'
    key=s.reserve(p,NOW)
    assert count(f,JobRun)==1 and count(f,JobCursor)==1
    assert s.inspect(p,NOW)['phase']=='reserved'
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    s.receive(key,body(),NOW+timedelta(seconds=1))
    assert s.inspect(p,NOW+timedelta(seconds=1))['phase']=='received'
    assert s.replay(key,NOW+timedelta(seconds=2))['supplierHttp']==0
    next_plan=plan(tmp_path,at=NOW+timedelta(days=1))
    state=s.inspect(next_plan,NOW+timedelta(days=1))
    assert state['phase']=='completed' and state['plan']==p
    assert state['normalized']['downloadResourceKey']=='512661'
    assert s.replay(key,NOW+timedelta(days=1))['readOnly']
    assert count(f,SourcePayload)==1


def test_failed_scope_frozen_and_other_fund_independent(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    assert s.inspect(plan(tmp_path,at=NOW+timedelta(days=1)),NOW+timedelta(days=1))['phase']=='failed'
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    assert s.reserve(plan(tmp_path,'fund-515220'),NOW+timedelta(seconds=2))!=key


def test_bad_saved_document_is_not_refetched_or_mistaken_for_proof(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    s.receive(key,b'<html>invalid but retained</html>',NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(seconds=3))['phase']=='received'
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    assert count(f,SourcePayload)==1 and count(f,JobRun)==1


@pytest.mark.parametrize('bad',['api','content','time','normalized','batch','request-count'])
def test_raw_run_and_batch_binding_tampering_is_rejected(database,tmp_path,bad):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    s.receive(key,body(),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    with f.begin() as session:
        row=session.get(JobRun,key);raw=session.get(SourcePayload,row.details['rawId'])
        if bad=='api':raw.api_name='wrong-source'
        elif bad=='content':raw.content_hash='0'*64
        elif bad=='time':raw.captured_at=NOW-timedelta(days=1)
        elif bad=='normalized':row.details={**row.details,'normalized':{**row.details['normalized'],'downloadResourceKey':'999999'}}
        elif bad=='batch':raw.batch_id='00000000-0000-0000-0000-000000000000'
        elif bad=='request-count':row.request_count=0
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=4))


def test_policy_change_clock_regression_and_unused_old_plan_are_blocked(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path)
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    s.reserve(p,NOW)
    bad=deepcopy(p);bad['limits']['maxHttpPerRun']=1;bad['fingerprint']=digest({k:v for k,v in bad.items() if k!='fingerprint'})
    with pytest.raises(ValueError):s.inspect(bad,NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.inspect(p,NOW-timedelta(seconds=1))


def test_last_shared_slot_counts_discovery_in_legacy_budget(database,tmp_path):
    _,f=database;s=store(f)
    from backend.tests.macro.test_official_daily_budget_switch import fill_day
    fill_day(f,tmp_path,total=3)
    s.reserve(plan(tmp_path),NOW)
    assert s.inspect(plan(tmp_path),NOW)['dailyBudget']['remaining']==0
    from backend.tests.macro.test_official_member_store import plan as old_plan,store as old_store
    p=old_plan(NOW)
    with pytest.raises(ValueError,match='DAILY_BUDGET'):old_store(f).reserve(p,p['policy']['documents'][0],NOW)


def test_moved_run_and_batch_timestamp_cannot_refund_shared_day(database,tmp_path):
    from app.db.models.raw import IngestionBatch
    from app.macro.official_member_budget import daily_budget
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    with f.begin() as session:
        session.get(JobRun,key).started_at=NOW-timedelta(days=1)
        session.get(IngestionBatch,key).started_at=NOW-timedelta(days=1)
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=2))
    with f() as session:
        with pytest.raises(ValueError):daily_budget(session,NOW+timedelta(seconds=2),4)
