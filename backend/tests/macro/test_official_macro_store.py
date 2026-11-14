from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_property_loans import directory, REPORT


def plan(group='householdCredit', now=NOW):
    return import_module('app.macro.official_macro_plan').build_official_macro_plan(group,policy(group),now)


def store(factory):
    return import_module('app.macro.official_macro_store').OfficialMacroStore(factory)


def complete_first(factory, p=None):
    p=p or plan();s=store(factory);doc=p['documents'][0]
    key=s.reserve(p,doc,NOW)
    s.receive(key,document(title='2026年前八个月金融统计数据报告'),NOW+timedelta(seconds=1))
    s.replay(key,NOW+timedelta(seconds=2))
    return s,p,key


def test_reservation_raw_then_core_and_same_target_immutable_reentry(database):
    _,factory=database;s=store(factory);p=plan();doc=p['documents'][0]
    key=s.reserve(p,doc,NOW)
    assert s.inspect(p,NOW)['status']=='blocked' and count(factory,MacroObservation)==0
    s.receive(key,document(title='2026年前八个月金融统计数据报告'),NOW+timedelta(seconds=1))
    assert count(factory,SourcePayload)==1 and count(factory,MacroObservation)==0
    s.replay(key,NOW+timedelta(seconds=2))
    with factory() as session:
        row=session.get(JobRun,key);before=deepcopy(row.details)
        core=session.scalar(select(MacroObservation))
        assert str(core.value)=='12100.0000000000' and core.observed_at.isoformat()=='2026-08-01'
        assert core.released_at.replace(tzinfo=NOW.tzinfo)==NOW+timedelta(seconds=2)
    result=s.replay(key,NOW+timedelta(seconds=3))
    assert result['supplierHttp']==0 and result['readOnly']
    with factory() as session:assert session.get(JobRun,key).details==before


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_or_failed_does_not_refund_or_move_to_next_week(database,failed):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['documents'][0],NOW)
    if failed:s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    later=NOW+timedelta(days=8);next_plan=plan(now=later)
    state=s.inspect(next_plan,later)
    assert state['status']=='blocked' and state['plan']==p
    with pytest.raises(ValueError):s.reserve(next_plan,next_plan['documents'][0],later)
    assert count(factory,JobCursor)==1 and count(factory,JobRun)==1


def test_saved_bad_raw_only_zero_http_replay_and_no_next_document(database):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['documents'][0],NOW)
    s.receive(key,b'<h1>bad</h1>',NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(seconds=2))['documents'][0]['phase']=='received'
    with pytest.raises(ValueError):s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    assert count(factory,MacroObservation)==0 and count(factory,SourcePayload)==1


def test_duplicate_or_out_of_order_reservation_blocked(database):
    _,factory=database;s=store(factory);p=plan()
    with pytest.raises(ValueError):s.reserve(p,p['documents'][1],NOW)
    s.reserve(p,p['documents'][0],NOW)
    with pytest.raises(ValueError):s.reserve(p,p['documents'][0],NOW)


@pytest.mark.parametrize('field', ['normalized','raw','binding','core','batch'])
def test_tampered_proof_is_rejected(database,field):
    _,factory=database;s,p,key=complete_first(factory)
    with factory.begin() as session:
        row=session.get(JobRun,key);d=deepcopy(row.details)
        if field=='normalized':d['normalized']['points'][0]['value']='999'
        elif field=='raw':
            raw=session.get(SourcePayload,d['rawId']);raw.payload={**raw.payload,'rawSha256':'a'*64}
        elif field=='binding':d['coreBindings'][0]['coreId']=99999
        elif field=='core':session.scalar(select(MacroObservation)).source_url='https://example.org/'
        else:
            from app.db.models.raw import IngestionBatch
            session.get(IngestionBatch,key).request_count=0
        row.details=d
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=3))


def test_clock_regression_and_policy_change_fail_closed(database):
    _,factory=database;s,p,key=complete_first(factory)
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=1))
    value=policy();value['limits']['maxHttpPerRun']=1
    changed=import_module('app.macro.official_macro_plan').build_official_macro_plan('householdCredit',value,NOW)
    with pytest.raises(ValueError):s.inspect(changed,NOW+timedelta(seconds=4))


def test_group_failure_does_not_block_an_independent_group(database):
    _,factory=database;s=store(factory);p=plan();key=s.reserve(p,p['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    housing=plan('tier1Housing')
    assert s.inspect(housing,NOW+timedelta(seconds=2))['status']=='new'
    assert s.reserve(housing,housing['documents'][0],NOW+timedelta(seconds=2))


def test_property_request_can_only_bind_to_its_completed_actual_directory(database):
    _,factory=database;s=store(factory);p=plan('propertyLoans');doc=p['documents'][0]
    key=s.reserve(p,doc,NOW)
    s.receive(key,directory([(REPORT,'2026年二季度金融机构贷款投向统计报告')]),NOW+timedelta(seconds=1))
    s.replay(key,NOW+timedelta(seconds=2))
    request=s.request_for(p,p['documents'][1],NOW+timedelta(seconds=3))
    assert request['url']==REPORT and request['period']=='2026-Q2' and request['directoryRunId']==key
    assert count(factory,MacroObservation)==0
    second=s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    with factory() as session:assert session.get(JobRun,second).details['request']==request


def test_no_observed_property_report_means_no_second_request_slot(database):
    _,factory=database;s=store(factory);p=plan('propertyLoans')
    key=s.reserve(p,p['documents'][0],NOW)
    s.receive(key,directory([]),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    assert s.request_for(p,p['documents'][1],NOW+timedelta(seconds=3)) is None
    with pytest.raises(ValueError):s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    assert count(factory,JobRun)==1


def test_changed_official_value_appends_revision_and_preserves_earlier_raw(database):
    _,factory=database;s,p,first=complete_first(factory)
    key=s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    s.receive(key,document(),NOW+timedelta(seconds=4));s.replay(key,NOW+timedelta(seconds=5))
    later=NOW+timedelta(days=8);next_plan=plan(now=later)
    key=s.reserve(next_plan,next_plan['documents'][0],later)
    s.receive(key,document(amount='1.20',title='2026年前八个月金融统计数据报告'),later+timedelta(seconds=1))
    s.replay(key,later+timedelta(seconds=2))
    assert s.inspect(next_plan,later+timedelta(seconds=3))['status']=='partial'
    with factory() as session:
        old=session.get(JobRun,first)
        core=session.get(MacroObservation,old.details['coreBindings'][0]['coreId'])
        assert str(core.value)=='12100.0000000000'
        assert core.valid_until.replace(tzinfo=NOW.tzinfo)==later+timedelta(seconds=2)
        assert old.details['normalized']['points'][0]['value']=='12100'
        assert len(session.scalars(select(MacroObservation).where(MacroObservation.observed_at==core.observed_at)).all())==2


def test_successful_directory_without_candidate_is_terminal_for_week_but_checks_next_week(database):
    _,factory=database;s=store(factory);p=plan('propertyLoans')
    key=s.reserve(p,p['documents'][0],NOW)
    s.receive(key,directory([]),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(seconds=3))['status']=='source_pending'
    for days in (8,15):
        later=NOW+timedelta(days=days);next_plan=plan('propertyLoans',later)
        state=s.inspect(next_plan,later)
        assert state['status']=='new' and state['plan']==next_plan
