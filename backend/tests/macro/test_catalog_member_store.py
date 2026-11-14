from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest
from sqlalchemy import select

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch,SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_catalog_member_policy import NOW,plan,value
from backend.tests.macro.test_official_member_store import plan as old_plan,store as old_store
from backend.tests.macro.test_official_members import workbook


def store(factory):return import_module('app.macro.catalog_member_store').CatalogMemberStore(factory)


def test_new_root_is_independent_and_durable_before_raw(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);doc=p['policy']['documents'][0]
    key=s.reserve(p,doc,NOW)
    assert s.inspect(p,NOW)['status']=='blocked' and count(f,SourcePayload)==0
    with f() as session:
        row=session.get(JobRun,key)
        assert row.job_name=='enterprise-official-catalog-v1:000905.SH' and row.request_count==1
        assert session.get(IngestionBatch,key).request_count==1
    with pytest.raises(ValueError):s.reserve(p,doc,NOW)


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_or_failed_target_freezes_but_other_target_can_reserve(database,tmp_path,failed):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,p['policy']['documents'][0],NOW)
    if failed:s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    later=NOW+timedelta(days=1);next_plan=plan(tmp_path,now=later)
    state=s.inspect(next_plan,later)
    assert state['status']=='blocked' and state['plan']==p
    with pytest.raises(ValueError):s.reserve(next_plan,next_plan['policy']['documents'][0],later)
    other=plan(tmp_path,'000510.SH',now=later)
    s.reserve(other,other['policy']['documents'][0],later)
    assert count(f,JobRun)==2


def test_bad_saved_raw_only_zero_http_replay_and_other_target_unblocked(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,p['policy']['documents'][0],NOW)
    s.receive(key,b'bad xls',NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(seconds=2))['documents'][0]['phase']=='received'
    other=plan(tmp_path,'000510.SH');s.reserve(other,other['policy']['documents'][0],NOW+timedelta(seconds=3))
    assert count(f,SourcePayload)==1 and count(f,JobRun)==2


def test_legacy_and_new_target_share_daily_budget_without_refunding_unknown(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);q=plan(tmp_path,'000510.SH')
    legacy=old_store(f);frozen=old_plan(NOW-timedelta(days=1))
    key=legacy.reserve(frozen,frozen['policy']['documents'][0],NOW)
    from backend.tests.macro.official_xls_fixture import compound_workbook
    legacy.receive(key,compound_workbook(),NOW+timedelta(seconds=1));legacy.replay(key,NOW+timedelta(seconds=2))
    s.reserve(p,p['policy']['documents'][0],NOW+timedelta(seconds=3))
    s.reserve(q,q['policy']['documents'][0],NOW+timedelta(seconds=4))
    # 没有冻结根的未知槽应阻断整个共享计数，不能当合法记录或退款。
    with f.begin() as session:
        session.add(JobRun(id='aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa',job_name='enterprise-official-catalog-v1:999999.SH',
            business_key='unknown',status='running',started_at=NOW+timedelta(seconds=5),request_count=1,row_count=0,details={}))
    budget=import_module('app.macro.official_member_budget')
    with f() as session:
        with pytest.raises(ValueError,match='BUDGET_INVALID'):
            budget.daily_budget(session,NOW+timedelta(seconds=6),4)
    # 旧入口也须检查共享预算，而非只检查自己日期根。
    next_old=old_plan(NOW)
    with pytest.raises(ValueError):legacy.reserve(next_old,next_old['policy']['documents'][0],NOW+timedelta(seconds=7))
    assert count(f,JobRun)==4


def test_budget_uses_shanghai_day_and_rejects_clock_regression(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);s.reserve(p,p['policy']['documents'][0],NOW)
    budget=import_module('app.macro.official_member_budget')
    with f() as session:
        assert budget.daily_budget(session,NOW+timedelta(hours=13),4)['used']==1
        assert budget.daily_budget(session,NOW+timedelta(hours=15),4)['used']==0
        with pytest.raises(ValueError):budget.daily_budget(session,NOW-timedelta(microseconds=1),4)


def test_static_root_policy_change_does_not_allow_new_day_or_document_id(database,tmp_path):
    _,f=database;s=store(f);p=plan(tmp_path);s.reserve(p,p['policy']['documents'][0],NOW)
    v=value();v['catalogEvidence']['targets'][0]['documents'][0]['id']='different-id'
    changed=plan(tmp_path,v=v,now=NOW+timedelta(days=1))
    with pytest.raises(ValueError):s.inspect(changed,NOW+timedelta(days=1))
    assert count(f,JobCursor)==count(f,JobRun)==1


@pytest.mark.parametrize('mutation',['root','raw','batch','normalized','timestamp'])
def test_tampering_and_clock_cannot_make_received_or_completed_trusted(database,tmp_path,mutation):
    _,f=database;s=store(f);p=plan(tmp_path);key=s.reserve(p,p['policy']['documents'][0],NOW)
    s.receive(key,b'bad source held',NOW+timedelta(seconds=1))
    with f.begin() as session:
        row=session.get(JobRun,key);d=deepcopy(row.details)
        if mutation=='root':session.delete(session.scalar(select(JobCursor)))
        elif mutation=='raw':session.get(SourcePayload,d['rawId']).content_hash='0'*64
        elif mutation=='batch':session.get(IngestionBatch,key).request_count=0
        elif mutation=='normalized':d['normalized']={'memberCount':1};row.details=d
        else:row.started_at=NOW+timedelta(seconds=5)
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=2))
