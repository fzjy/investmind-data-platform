from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from app.db.models.ops import JobRun,JobCursor
from app.db.models.raw import SourcePayload,IngestionBatch
from app.macro.extensions.plan import digest
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_issuer_contract_samples import NOW,plan


def store(factory):return import_module('app.macro.issuer_sample_store').IssuerSampleStore(factory)


def test_original_only_no_normalization_or_recollection_next_day(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    assert count(f,JobCursor)==1 and count(f,JobRun)==1 and count(f,SourcePayload)==0
    s.receive(key,b'<html>opaque source, not an asserted fund mapping</html>',NOW+timedelta(seconds=1))
    state=s.inspect(plan(tmp_path,at=NOW+timedelta(days=1)),NOW+timedelta(days=1))
    assert state['phase']=='received' and state['plan']==p and not state['proofReady'] and state['normalized'] is None
    assert state['sample']['byteLength']>0 and len(state['sample']['rawSha256'])==64
    assert not any(k in state['sample'] for k in ('body','bodyBase64','text','normalized'))
    with pytest.raises(ValueError,match='NOT_IMPLEMENTED'):s.replay(key,NOW+timedelta(days=1))
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    assert count(f,SourcePayload)==1 and count(f,JobRun)==1


@pytest.mark.parametrize('bad',['content','raw-api','raw-time','batch','request-count','fake-completed','moved-reservation'])
def test_receipt_and_temporal_tampering_rejected(database,tmp_path,bad):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,NOW)
    s.receive(key,b'opaque source',NOW+timedelta(seconds=1))
    with f.begin() as session:
        row=session.get(JobRun,key);raw=session.get(SourcePayload,row.details['rawId']);batch=session.get(IngestionBatch,key)
        if bad=='content':raw.content_hash='0'*64
        elif bad=='raw-api':raw.api_name='official-fund-contract'
        elif bad=='raw-time':raw.captured_at=NOW-timedelta(days=1)
        elif bad=='batch':raw.batch_id='00000000-0000-0000-0000-000000000000'
        elif bad=='request-count':row.request_count=0
        elif bad=='fake-completed':
            row.details={**row.details,'phase':'completed','normalized':{}};row.status='completed';row.finished_at=NOW+timedelta(seconds=1)
            batch.status='completed';batch.finished_at=row.finished_at
        elif bad=='moved-reservation':row.started_at=NOW-timedelta(days=1);batch.started_at=row.started_at
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=3))


def test_fail_unknown_policy_change_and_time_guard(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path)
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))
    key=s.reserve(p,NOW)
    assert s.inspect(p,NOW)['phase']=='reserved'
    with pytest.raises(ValueError):s.reserve(p,NOW)
    with pytest.raises(ValueError):s.inspect(p,NOW-timedelta(seconds=1))
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    assert s.inspect(plan(tmp_path,at=NOW+timedelta(days=1)),NOW+timedelta(days=1))['phase']=='failed'
    changed=deepcopy(p);changed['limits']['maxHttpPerRun']=1
    changed['fingerprint']=digest({k:v for k,v in changed.items() if k!='fingerprint'})
    with pytest.raises(ValueError):s.inspect(changed,NOW+timedelta(seconds=2))
    with pytest.raises(ValueError):s.reserve(p,NOW+timedelta(days=1))


def test_family_budget_counts_sample_and_rejects_time_refund(database,tmp_path):
    from app.macro.official_member_budget import daily_budget
    from backend.tests.macro.test_official_member_store import plan as old_plan,store as old_store
    _,f=database;s=store(f);p=plan(tmp_path)
    from backend.tests.macro.test_official_daily_budget_switch import fill_day
    fill_day(f,tmp_path,total=3)
    key=s.reserve(p,NOW)
    with f() as session:assert daily_budget(session,NOW,4)['remaining']==0
    old=old_plan(NOW)
    with pytest.raises(ValueError,match='DAILY_BUDGET'):old_store(f).reserve(old,old['policy']['documents'][0],NOW)
    with f.begin() as session:
        session.get(JobRun,key).started_at=NOW-timedelta(days=1);session.get(IngestionBatch,key).started_at=NOW-timedelta(days=1)
    with f() as session:
        with pytest.raises(ValueError):daily_budget(session,NOW+timedelta(seconds=1),4)


@pytest.mark.parametrize('kind',['sample','discovery'])
def test_moving_root_with_reservation_cannot_detach_original_plan_and_refund(database,tmp_path,kind):
    from sqlalchemy import select
    from app.macro.official_member_budget import daily_budget
    _,f=database;s=store(f);p=plan(tmp_path)
    if kind=='discovery':
        from app.macro.fund_contract_store import FundContractStore
        from backend.tests.macro.test_fund_contract_discovery import plan as discovery_plan
        s=FundContractStore(f);p=discovery_plan(tmp_path)
    key=s.reserve(p,NOW)
    with f.begin() as session:
        row=session.get(JobRun,key);root=session.scalar(select(JobCursor).where(JobCursor.job_name==row.job_name))
        moved=NOW-timedelta(days=1);row.started_at=moved;session.get(IngestionBatch,key).started_at=moved;root.updated_at=moved
        changed=deepcopy(p);changed['firstBusinessDate']=moved.date().isoformat()
        changed['fingerprint']=digest({k:v for k,v in changed.items() if k!='fingerprint'})
        root.cursor_value={'plan':changed}
    with f() as session:
        with pytest.raises(ValueError):daily_budget(session,NOW,4)
