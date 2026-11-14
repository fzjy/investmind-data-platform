from datetime import datetime,timedelta,timezone
from importlib import import_module

import pytest
from sqlalchemy import select

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_official_member_policy import policy
from backend.tests.macro.official_xls_fixture import compound_workbook

NOW=datetime(2026,10,25,4,tzinfo=timezone.utc)


def plan(at=NOW):
    from app.macro.official_member_policy import OfficialMemberPolicy
    p=policy();p['collectionEnabled']=True;p['bootstrapTargetCodes']=['000300.SH'];p['documents']=p['documents'][:1]
    return import_module('app.macro.official_member_store').build_plan(OfficialMemberPolicy.model_validate(p),'a'*64,at)


def store(factory):return import_module('app.macro.official_member_store').OfficialMemberStore(factory)


def test_committed_reservation_raw_then_immutable_members_zero_http_replay(database):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['policy']['documents'][0],NOW)
    assert s.inspect(p,NOW)['status']=='blocked'
    assert count(factory,JobRun)==1 and count(factory,SourcePayload)==0
    s.receive(key,compound_workbook(),NOW+timedelta(seconds=1))
    assert s.inspect(p,NOW+timedelta(seconds=1))['status']=='partial'
    result=s.replay(key,NOW+timedelta(seconds=2));assert result['status']=='completed'
    with factory() as session:
        row=session.get(JobRun,key);before=dict(row.details)
        assert row.details['normalized']['members']==['000001.SZ','600000.SH']
    again=s.replay(key,NOW+timedelta(seconds=3))
    assert again['readOnly'] and again['supplierHttp']==0
    with factory() as session:assert session.get(JobRun,key).details==before
    assert s.inspect(p,NOW+timedelta(seconds=3))['status']=='completed'


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_or_failed_reservation_cannot_be_retried_even_on_new_day(database,failed):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['policy']['documents'][0],NOW)
    if failed:s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    later=NOW+timedelta(days=1);state=s.inspect(plan(later),later)
    assert state['status']=='blocked' and state['plan']==p
    with pytest.raises(ValueError):s.reserve(plan(later),p['policy']['documents'][0],later)
    assert count(factory,JobRun)==1 and count(factory,JobCursor)==1


def test_bad_saved_document_keeps_raw_without_issuing_new_slot(database):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['policy']['documents'][0],NOW)
    s.receive(key,b'<html>not xls</html>',NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=2))
    assert count(factory,SourcePayload)==1 and count(factory,JobRun)==1
    assert s.inspect(p,NOW+timedelta(seconds=2))['documents'][0]['phase']=='received'
    with pytest.raises(ValueError):s.reserve(p,p['policy']['documents'][0],NOW+timedelta(seconds=3))


def test_tampered_raw_or_member_proof_is_not_trusted(database):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['policy']['documents'][0],NOW)
    s.receive(key,compound_workbook(),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    with factory.begin() as session:
        row=session.get(JobRun,key);d=row.details
        row.details={**d,'normalized':{**d['normalized'],'members':['600001.SH']}}
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=3))


def test_current_day_policy_drift_and_clock_regression_rejected(database):
    _,factory=database;s=store(factory);p=plan();s.reserve(p,p['policy']['documents'][0],NOW)
    changed=plan();changed['policy']['maxHttpPerDay']=3
    with pytest.raises(ValueError):s.inspect(changed,NOW)
    with pytest.raises(ValueError):s.inspect(p,NOW-timedelta(days=1))
