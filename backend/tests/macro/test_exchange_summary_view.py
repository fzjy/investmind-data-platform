from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_exchange_summary_plan import build
from backend.tests.macro.test_exchange_summary_store import NOW,store,complete


def collect(factory,group='marginBalances',at=NOW,amounts=None,fail_bse=False,lookback=48):
    from backend.tests.macro.test_exchange_summary_config import policy
    config=policy(group);config['lookbackPeriods']=lookback
    p=build(at.isoformat(),group=group,p=config);s=store(factory)
    amounts=amounts or (['100000000','200000000','999999999'] if group=='marginBalances' else ['10','20'])
    for i,doc in enumerate(p['documents']):
        start=at+timedelta(seconds=i*3)
        if fail_bse and i==2:
            key=s.reserve(p,doc,start);s.fail(key,'EXTENSION_PERMISSION_ERROR',start+timedelta(seconds=1));break
        fields=doc['query']['fields'];data={'fields':fields,'items':[[doc['query']['params'].get(k,amounts[i]) for k in fields]]}
        complete(s,p,doc,start,data)
    return p


def view(factory,at=NOW+timedelta(seconds=20)):
    return import_module('app.macro.exchange_summary_view').load_exchange_summary_view(factory,at)


def test_same_day_four_inputs_explicit_unit_conversion_and_bse_excluded(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv')
    v=view(f);r=v['monthly'][-1]
    assert r['tradeDate']=='2026-10-22' and r['marginBalanceYuan']=='300000000' and r['floatMvYiYuan']=='30'
    assert r['value']=='10' and r['bseBalanceYuan']=='999999999'
    assert v['basis']=='normalization-visible-restated-history-not-strict-pit'
    assert len(v['monthly'])==48 and len(v['missingPeriods'])==47
    assert import_module('app.macro.exchange_summary_view').exchange_summary_view_valid(v)


def test_bse_failure_does_not_hide_complete_shanghai_shenzhen_ratio(database):
    _,f=database;collect(f,fail_bse=True);collect(f,'aShareFloatMv')
    v=view(f)
    assert v['monthly'][-1]['value']=='10' and v['monthly'][-1]['bseBalanceYuan'] is None
    assert v['groups']['marginBalances']['latestAttempt']['errorCode']=='EXTENSION_PERMISSION_ERROR'


def test_different_trade_days_never_join_or_fill_previous_month(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv',NOW+timedelta(days=1))
    v=view(f, NOW+timedelta(days=1,seconds=20))
    assert v['monthly'][-1]['value'] is None and len(v['missingPeriods'])==48


@pytest.mark.parametrize('numerator,denominator,expected,reason',[
    (['0','0','0'],['10','20'],'0',None),
    (['1','2','3'],['0','0'],None,'NONPOSITIVE_DENOMINATOR')])
def test_valid_zero_is_not_missing_and_zero_denominator_never_divides(database,numerator,denominator,expected,reason):
    _,f=database;collect(f,amounts=numerator);collect(f,'aShareFloatMv',amounts=denominator)
    r=view(f)['monthly'][-1]
    assert r['value']==expected and r['reason']==reason


def test_exact_cutoff_excludes_normalization_not_yet_finished(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv')
    v=view(f,NOW+timedelta(seconds=1))
    assert v['monthly'][-1]['value'] is None
    assert v['groups']['marginBalances']['targets'][0]['documents']==[]


def test_latest_failed_target_keeps_previous_complete_same_day_monthly_pair(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv')
    later=NOW+timedelta(days=1);p=build(later.isoformat());s=store(f)
    key=s.reserve(p,p['documents'][0],later);s.fail(key,'EXTENSION_NETWORK_ERROR',later+timedelta(seconds=1))
    v=view(f,later+timedelta(seconds=10))
    assert v['monthly'][-1]['value']=='10' and v['monthly'][-1]['tradeDate']=='2026-10-22'
    assert v['groups']['marginBalances']['latestAttempt']['plan']['targetDate']=='2026-10-23'


def test_shortening_window_uses_latest_policy_and_stays_self_validating(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv')
    later=NOW+timedelta(days=1)
    collect(f,at=later,lookback=6);collect(f,'aShareFloatMv',later,lookback=6)
    v=view(f,later+timedelta(seconds=20));module=import_module('app.macro.exchange_summary_view')
    assert v['window']['requestedPeriods']==6 and len(v['monthly'])==6 and module.exchange_summary_view_valid(v)


def test_cross_group_duplicate_core_identity_is_rejected(database):
    _,f=database;collect(f);collect(f,'aShareFloatMv');v=view(f)
    v['groups']['aShareFloatMv']['targets'][0]['documents'][0]['coreBindings'][0]['coreId']=v['groups']['marginBalances']['targets'][0]['documents'][0]['coreBindings'][0]['coreId']
    module=import_module('app.macro.exchange_summary_view');v['evidenceHash']=module.evidence_hash(v)
    assert not module.exchange_summary_view_valid(v)


@pytest.mark.parametrize('field',['unit','date','ratio','hash','run','raw','microsecond','extra','calendar','budget','binding'])
def test_rehashed_tampering_is_rejected(database,field):
    _,f=database;collect(f);collect(f,'aShareFloatMv');v=view(f)
    module=import_module('app.macro.exchange_summary_view');target=v['groups']['marginBalances']['targets'][0];doc=target['documents'][0]
    if field=='unit':doc['normalized']['points'][0]['unit']='亿元'
    elif field=='date':doc['businessData']['items'][0][0]='20261023'
    elif field=='ratio':v['monthly'][-1]['value']='20'
    elif field=='hash':doc['businessSha256']='0'*64
    elif field=='run':doc['runId']='00000000-0000-5000-8000-000000000000'
    elif field=='raw':doc['businessData']['items'][0][2]='999'
    elif field=='microsecond':doc['completedAt']='2026-10-25T16:30:00.000001+00:00'
    elif field=='extra':doc['token']='forbidden'
    elif field=='calendar':target['plan']['calendarEvidence']['rows'][0]['open']=False
    elif field=='budget':target['plan']['limits']['maxHttpPerPeriod']=9
    else:doc['coreBindings'][0]['value']='2'
    v['evidenceHash']=module.evidence_hash(v)
    assert not module.exchange_summary_view_valid(v)
