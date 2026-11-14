from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_official_macro_store import complete_first, plan, store
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_special_bonds import quota, monthly, actual_briefing
from backend.tests.macro.test_official_tier1_housing import document as housing_document


def view(factory,now=NOW+timedelta(seconds=20)):
    return import_module('app.macro.official_macro_view').load_official_macro_view(factory,now)


def test_no_ledger_returns_none_and_does_not_change_old_extension_contract(database):
    _,factory=database
    assert view(factory) is None


def test_household_missing_previous_month_remains_null_and_analysis_cutoff_exact(database):
    _,factory=database;complete_first(factory)
    value=view(factory)
    assert value['analysisDate']=='2026-10-25' and value['basis']=='normalization-visible-restated-history-not-strict-pit'
    group=value['groups']['householdCredit']
    assert len(group['series'][0]['points'])==48 and len(group['series'][1]['points'])==48
    assert group['series'][1]['latest'] is None
    assert len(group['series'][1]['missingPeriods'])==48
    assert view(factory,NOW+timedelta(seconds=1))['groups']['householdCredit']['documents']==[]


def test_special_preserves_reported_progress_revisions_and_events(database):
    _,factory=database;p=plan('specialBonds');s=store(factory)
    for i,body in enumerate((quota(),monthly(),actual_briefing())):
        at=NOW+timedelta(seconds=i*3)
        key=s.reserve(p,p['documents'][i],at);s.receive(key,body,at+timedelta(seconds=1));s.replay(key,at+timedelta(seconds=2))
    value=view(factory);group=value['groups']['specialBonds']
    assert group['progress']['period']=='2026-09-01'
    assert group['progress']['quota']=='44000' and group['progress']['revisionConflict']
    assert group['progress']['directMonth']=='6339' and group['progress']['differenceDiscrepancy']=='24'
    assert group['progress']['reportedProgress']=='81'
    assert len(group['documents'])==3 and group['documents'][2]['normalized']['events'][0]['amount']=='2500'
    assert group['series'][0]['latest']=={'month':'2026-09-01','value':'6339'}
    assert len(group['series'][0]['missingPeriods'])==46


@pytest.mark.parametrize('field',['future','unit','source','period','series','derived','raw','url','role','budget','extra'])
def test_public_official_contract_rejects_tampering_even_when_rehashed(database,field):
    _,factory=database;complete_first(factory);value=view(factory)
    module=import_module('app.macro.official_macro_view')
    assert module.official_macro_view_valid(value)
    group=value['groups']['householdCredit'];doc=group['documents'][0];point=doc['normalized']['points'][0]
    if field=='future':doc['completedAt']='2026-10-26T00:00:00+00:00'
    elif field=='unit':point['unit']='万亿元'
    elif field=='source':doc['normalized']['source']='nbs'
    elif field=='period':point['period']='2026-08-02'
    elif field=='series':point['seriesId']='cn.credit.property_loans_yoy'
    elif field=='derived':group['series'][1]['points'][-1]['value']='0'
    elif field=='raw':doc['normalized']['rawSha256']='not-hash'
    elif field=='url':doc['request']['url']='https://example.org/'
    elif field=='role':doc['normalized']['role']='special-issuance'
    elif field=='budget':group['plan']['limits']['maxHttpPerPeriod']=100
    else:value['extra']=True
    value['evidenceHash']=import_module('app.macro.extensions.plan').digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not module.official_macro_view_valid(value)


def test_failed_source_stays_explicit_without_fabricated_zero_or_usable_points(database):
    _,factory=database;s=store(factory);p=plan('propertyLoans')
    key=s.reserve(p,p['documents'][0],NOW);s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    group=view(factory)['groups']['propertyLoans']
    assert group['latestAttempt']['status']=='failed' and group['latestAttempt']['errorCode']=='EXTENSION_AUTH_ERROR'
    assert group['documents']==[] and all(s['latest'] is None for s in group['series'])
    assert len(group['series'][0]['missingPeriods'])==16  # 48个月含16个季度，不把其余32个月算成缺失观测。


def test_new_week_failure_keeps_last_complete_official_source_version(database):
    from backend.tests.macro.test_official_household_credit import document
    _,factory=database;s,p,_=complete_first(factory)
    key=s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    s.receive(key,document(),NOW+timedelta(seconds=4));s.replay(key,NOW+timedelta(seconds=5))
    before=view(factory)['groups']['householdCredit']
    later=NOW+timedelta(days=8);next_plan=plan(now=later)
    key=s.reserve(next_plan,next_plan['documents'][0],later)
    s.fail(key,'EXTENSION_AUTH_ERROR',later+timedelta(seconds=1))
    after=view(factory,later+timedelta(seconds=2))['groups']['householdCredit']
    assert after['documents']==before['documents'] and after['series']==before['series']
    assert after['latestAttempt']['status']=='failed' and after['latestAttempt']['plan']==next_plan


def test_optional_official_contract_is_bound_to_extension_13_without_changing_12(tmp_path):
    from app.macro.extensions.view import _empty,build_extension_view,extension_view_valid
    from backend.tests.macro.test_fiscal_view import controlled_fiscal_view
    from backend.tests.e2e.export_controlled_official_macro import cases
    inputs={'sources':{key:_empty(key) for key in ('usdcnh','nominalGdp','vix')},'fiscal':controlled_fiscal_view(tmp_path)}
    before=build_extension_view(inputs)
    value=build_extension_view({**inputs,'official':cases()['full']})
    assert value['schemaVersion']=='1.3' and value['official']['groups']['specialBonds']['progress']['directMonth']=='6339'
    assert before['schemaVersion']=='1.2' and extension_view_valid(before) and extension_view_valid(value)
    changed=deepcopy(value);changed['official']['groups']['specialBonds']['progress']['quota']='46500'
    assert not extension_view_valid(changed)


def test_official_contract_does_not_require_government_bond_ledger():
    from app.macro.extensions.view import _empty,build_extension_view,extension_view_valid
    from backend.tests.e2e.export_controlled_official_macro import cases
    value=build_extension_view({'sources':{key:_empty(key) for key in ('usdcnh','nominalGdp','vix')},'official':cases()['full']})
    assert value['schemaVersion']=='1.3' and value['fiscal'] is None and extension_view_valid(value)
