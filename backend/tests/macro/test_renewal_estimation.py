from copy import deepcopy
from datetime import date
from fractions import Fraction
from importlib import import_module

import pytest

FIELDS=('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')


def module():return import_module('app.macro.renewal_estimation')


def observation(period, values=('30','10','10'), *, reason=None, available='2024-07-20'):
    return {'period':period,'symbol':'000000.SZ','availableDate':available,'sourceKey':'cashflow:'+period,'sourceHash':'a'*64,
        'fields':dict(zip(FIELDS,values,strict=True)),'reason':reason}


def choose(period, observations):return module().choose_depreciation(date.fromisoformat(period),observations)


def amount(result):
    v=result['value'];return Fraction(int(v['numerator']),int(v['denominator'])) if v else None


def test_reported_quarter_difference_has_priority_and_preserves_component_values():
    values={'2024-06-30':observation('2024-06-30',('90','30','30')),
        '2024-03-31':observation('2024-03-31')}
    before=deepcopy(values);r=choose('2024-06-30',values)
    assert r['method']=='reported-difference' and r['estimated'] is False
    assert amount(r)==100 and len(r['observations'])==2 and values==before
    assert module().decision_valid(r,date(2024,8,1))


def test_q1_direct_and_valid_zero_are_not_replaced_by_old_positive():
    r=choose('2024-03-31',{'2024-03-31':observation('2024-03-31',('0','0','0')),
        '2023-12-31':observation('2023-12-31')})
    assert r['method']=='reported-q1' and amount(r)==0 and not r['estimated']


def test_h1_average_when_previous_null_is_explicit_and_exact():
    r=choose('2024-06-30',{'2024-06-30':observation('2024-06-30',('1','1','1')),
        '2024-03-31':observation('2024-03-31',(None,None,None),reason='rea-supplement-returned-null')})
    assert r['method']=='cumulative-average' and r['estimated'] is True
    assert r['sourcePeriod']=='2024-06-30' and r['divisor']==2 and amount(r)==Fraction(3,2)


def test_missing_q3_can_use_h1_but_not_future_fy():
    r=choose('2024-09-30',{'2024-06-30':observation('2024-06-30'),
        '2024-12-31':observation('2024-12-31',('999','999','999'))})
    assert r['method']=='cumulative-average' and r['sourcePeriod']=='2024-06-30'
    assert amount(r)==25 and all(o['period']<='2024-09-30' for o in r['observations'])


def test_previous_year_fy_is_maximum_lookback_and_older_is_not_used():
    r=choose('2024-03-31',{'2023-12-31':observation('2023-12-31')})
    assert r['method']=='previous-year-average' and amount(r)==Fraction(25,2)
    assert choose('2024-03-31',{'2022-12-31':observation('2022-12-31')})['value'] is None


@pytest.mark.parametrize('bad', ['-1','NaN','Infinity',True,'1e100', '9'*81])
def test_invalid_or_negative_current_component_is_not_hidden_by_estimation(bad):
    r=choose('2024-06-30',{'2024-06-30':observation('2024-06-30',(bad,None,None)),
        '2023-12-31':observation('2023-12-31')})
    assert r['value'] is None and r['reason'].startswith('invalid-depreciation')


def test_negative_reported_component_difference_does_not_become_h1_average():
    r=choose('2024-06-30',{'2024-06-30':observation('2024-06-30',('90','0','30')),
        '2024-03-31':observation('2024-03-31')})
    assert r['value'] is None and r['reason']=='negative-quarter-depreciation'


@pytest.mark.parametrize('reason',['revision-conflict','industry-history-missing-or-changed','rea-supplement-not-visible','incompatible-company-type'])
def test_substantive_rejection_is_not_treated_as_ordinary_missing(reason):
    r=choose('2024-06-30',{'2024-06-30':observation('2024-06-30',reason=reason),
        '2023-12-31':observation('2023-12-31')})
    assert r['value'] is None and r['reason']==reason


@pytest.mark.parametrize('damage',['flag','value','divisor','future-visible','wrong-period','extra-source'])
def test_proof_reconstruction_rejects_damage(damage):
    r=choose('2024-06-30',{'2024-06-30':observation('2024-06-30')})
    assert module().decision_valid(r,date(2024,8,1))
    if damage=='flag':r['estimated']=False
    elif damage=='value':r['value']['numerator']='999'
    elif damage=='divisor':r['divisor']=0
    elif damage=='future-visible':r['observations'][0]['availableDate']='2024-08-02'
    elif damage=='wrong-period':r['sourcePeriod']='2024-09-30'
    else:r['observations'].append(observation('2023-12-31'))
    assert not module().decision_valid(r,date(2024,8,1))


def enabled(count=5):
    from dataclasses import replace
    from backend.tests.macro.test_enterprise_rea import controlled
    inputs=controlled(count)
    rows=tuple(replace(r,values={**r.values,**{f:None for f in FIELDS}})
        if r.table=='cashflow' and r.end_date.month==3 else r for r in inputs.financials)
    return replace(inputs,financials=rows,renewal_estimation_enabled=True)


def test_projection_estimation_has_member_evidence_and_only_changes_renewal():
    from dataclasses import replace
    from backend.tests.macro.test_enterprise_rea import build,metric
    inputs=enabled();new=build(inputs);old=build(replace(inputs,renewal_estimation_enabled=False))
    assert new['version']=='macro-enterprise-rea-v2' and old['version']=='macro-enterprise-rea-v1'
    m=metric(new,'renewal');p=m['points'][-1];q=m['quality'][-1]
    assert p['value']==pytest.approx(8/3) and q['sampleCount']==5
    e=q['depreciationEvidence'];assert e['estimatedMembers']==list(inputs.symbols)
    assert len(e['members'])==5 and all(r['current']['method']=='cumulative-average' for r in e['members'])
    assert [r for r in new['series'] if r['id']!='renewal']==[r for r in old['series'] if r['id']!='renewal']
    assert metric(old,'renewal')['points']==[]


def test_estimation_keeps_capex_strict_and_dynamic_member_denominator():
    from dataclasses import replace
    from backend.tests.macro.test_enterprise_rea import build,metric
    inputs=enabled(61)
    rows=tuple(replace(r,values={**r.values,'c_pay_acq_const_fiolta':None})
        if r.symbol==inputs.symbols[0] and r.table=='cashflow' and r.end_date.month==3 else r for r in inputs.financials)
    q=metric(build(replace(inputs,financials=rows)),'renewal')['quality'][-1]
    assert q['sampleCount']==60 and q['candidateCount']==61
    assert inputs.symbols[0] not in q['depreciationEvidence']['estimatedMembers']


def test_normal_subset_inherits_mode_without_changing_legacy_hash():
    from dataclasses import replace
    from app.macro.enterprise_contract import public_enterprise_analysis
    from app.macro.enterprise_industries import subset_enterprise_inputs
    from app.macro.enterprise import EnterprisePolicy
    inputs=enabled()
    policy=EnterprisePolicy(min_samples=1)
    old=public_enterprise_analysis(replace(inputs,renewal_estimation_enabled=False),date(2024,8,1),policy,rea_enabled=True)
    new=public_enterprise_analysis(inputs,date(2024,8,1),policy,rea_enabled=True)
    assert {k:v for k,v in old.items() if k!='rea'}=={k:v for k,v in new.items() if k!='rea'}
    child=subset_enterprise_inputs(inputs,inputs.symbols[:1],['2024-06-30'])
    assert child.renewal_estimation_enabled is True


@pytest.mark.parametrize('kind',['missing-member','wrong-estimate-list','amount','source-period','hash','future'])
def test_new_projection_rejects_rebuilt_bad_evidence(kind):
    from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
    from app.macro.enterprise import EnterprisePolicy
    inputs=enabled();parent=public_enterprise_analysis(inputs,date(2024,8,1),EnterprisePolicy(min_samples=1),rea_enabled=True)
    assert valid_enterprise_analysis(parent)
    m=next(m for m in parent['rea']['series'] if m['id']=='renewal');q=m['quality'][-1];e=q['depreciationEvidence']
    if kind=='missing-member':e['members'].pop()
    elif kind=='wrong-estimate-list':e['estimatedMembers']=[]
    elif kind=='amount':e['members'][0]['current']['value']['numerator']='999'
    elif kind=='source-period':e['members'][0]['current']['sourcePeriod']='2024-09-30'
    elif kind=='future':e['members'][0]['current']['observations'][0]['availableDate']='2024-08-02'
    else:e['evidenceHash']='0'*64
    if kind!='hash':e['evidenceHash']=module().evidence_hash(q['period'],e['members'])
    assert not valid_enterprise_analysis(parent)


@pytest.mark.parametrize('damage',['negative','invalid','industry','type','conflict','future-supplement','source-mismatch'])
def test_integration_substantive_previous_rejection_does_not_estimate(damage):
    from dataclasses import replace, asdict
    from app.macro.enterprise import ReaFieldInput, IndustryInput, _hash
    from backend.tests.macro.test_enterprise_rea import build,metric
    inputs=enabled(1);rows=list(inputs.financials);extra=()
    index=next(i for i,r in enumerate(rows) if r.table=='cashflow' and r.end_date==date(2024,3,31))
    row=rows[index]
    if damage in ('negative','invalid'):
        rows[index]=replace(row,values={**row.values,FIELDS[0]:'-1' if damage=='negative' else 'NaN'})
    elif damage=='type':rows[index]=replace(row,comp_type='2')
    elif damage=='conflict':rows.append(replace(row,source_key='conflict',values={**row.values,FIELDS[0]:'99'}))
    elif damage=='industry':
        inputs=replace(inputs,industries=(IndustryInput(row.symbol,'801110.SI',date(2020,1,1),date(2024,3,31)),
            IndustryInput(row.symbol,'801120.SI',date(2024,4,1),None)))
    else:
        rows[index]=replace(row,values={k:v for k,v in row.values.items() if k not in FIELDS})
        extra=tuple(ReaFieldInput(row.source_key,'0'*64 if damage=='source-mismatch' else _hash(asdict(rows[index])),f,
            'matched','1','a'*64,date(2024,8,2) if damage=='future-supplement' else date(2024,7,1),'req') for f in FIELDS)
    q=metric(build(replace(inputs,financials=tuple(rows),rea_fields=extra)),'renewal')['quality'][-1]
    assert q['sampleCount']==0 and q['excluded'][inputs.symbols[0]] not in ('missing-input','missing-depreciation-source')
    assert q['depreciationEvidence']['members']==[]


def test_current_missing_uses_visible_prior_fy_and_not_future_report():
    from dataclasses import replace
    from backend.tests.macro.test_enterprise_rea import build,metric
    inputs=enabled(1)
    annual=tuple(replace(r,end_date=date(2023,12,31),ann_date=date(2024,2,1),source_key='fy:'+r.source_key,
        values={**r.values,**dict.fromkeys(FIELDS,'8')}) for r in inputs.financials if r.table=='cashflow' and r.end_date==date(2023,6,30))
    rows=tuple(replace(r,values={**r.values,**dict.fromkeys(FIELDS,None)})
        if r.table=='cashflow' and r.end_date==date(2024,6,30) else r for r in inputs.financials)+annual
    q=metric(build(replace(inputs,financials=rows)),'renewal')['quality'][-1]
    assert q['sampleCount']==1
    d=q['depreciationEvidence']['members'][0]['current']
    assert d['method']=='previous-year-average' and d['sourcePeriod']=='2023-12-31' and amount(d)==6


def test_invisible_earlier_report_is_not_ordinary_missing_fallback():
    from dataclasses import replace
    from app.macro.enterprise_rea import build_rea_projection
    from app.macro.enterprise import EnterprisePolicy
    inputs=enabled(1);rows=[]
    for r in inputs.financials:
        if r.table=='cashflow' and r.end_date==date(2024,3,31):r=replace(r,ann_date=date(2024,11,2))
        if r.table=='cashflow' and r.end_date==date(2024,6,30):r=replace(r,values={**r.values,**dict.fromkeys(FIELDS,None)})
        rows.append(r)
        if r.end_date.month==6:
            rows.append(replace(r,end_date=date(r.end_date.year,9,30),ann_date=date(r.end_date.year,10,10),source_key='q3:'+r.source_key,
                values={**r.values,'c_pay_acq_const_fiolta':60} if r.table=='cashflow' else r.values))
        if r.table=='cashflow' and r.end_date==date(2023,6,30):
            rows.append(replace(r,end_date=date(2023,12,31),ann_date=date(2024,2,1),source_key='fy',values={**r.values,**dict.fromkeys(FIELDS,'8')}))
    result=build_rea_projection(replace(inputs,financials=tuple(rows)),date(2024,11,1),EnterprisePolicy(min_samples=1),{'inputHash':'a'*64})
    q=result['series'][4]['quality'][-1]
    assert q['period']=='2024-09-30' and q['sampleCount']==0
    assert q['excluded'][inputs.symbols[0]]=='depreciation-source-not-visible'


def test_swapping_member_decisions_and_rehashing_is_rejected():
    from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
    from app.macro.enterprise import EnterprisePolicy
    parent=public_enterprise_analysis(enabled(2),date(2024,8,1),EnterprisePolicy(min_samples=1),rea_enabled=True)
    e=parent['rea']['series'][4]['quality'][-1]['depreciationEvidence']
    e['members'][0]['current'],e['members'][1]['current']=e['members'][1]['current'],e['members'][0]['current']
    e['evidenceHash']=module().evidence_hash('2024-06-30',e['members'])
    assert not valid_enterprise_analysis(parent)


def test_industry_child_cannot_downgrade_estimates_to_strict_v1():
    from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
    from app.macro.enterprise import EnterprisePolicy
    parent=public_enterprise_analysis(enabled(),date(2024,8,1),EnterprisePolicy(min_samples=1),rea_enabled=True,industry_enabled=True)
    assert valid_enterprise_analysis(parent)
    child=parent['industryComparison']['groups'][0]['analysis']['rea']
    child.update(version='macro-enterprise-rea-v1',depreciationMode='strict-single-quarter-three-components-no-estimates')
    for q in child['series'][4]['quality']:q.pop('depreciationEvidence')
    assert not valid_enterprise_analysis(parent)
