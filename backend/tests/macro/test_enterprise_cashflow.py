"""企业现金卡只使用同组单季度金额，不误用同比基数或个股平均。"""
from dataclasses import replace
from datetime import date

import pytest

from app.macro.enterprise import EnterprisePolicy, FinancialRecord, build_enterprise_analysis
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_white_strip import enabled

AS_OF=date(2024,5,31)
POLICY=EnterprisePolicy(min_samples=1,max_quarters=1)


def cash_inputs(cfo=(100000000,900000000),capex=(50000000,150000000)):
    data=enabled()
    rows=tuple(FinancialRecord('cashflow',s,date(2024,3,31),date(2024,4,30),None,'1','1','1',
        {'n_cashflow_act':cfo[i],'c_pay_acq_const_fiolta':capex[i]},f'cash:{s}:2024Q1') for i,s in enumerate(data.symbols))
    return replace(data,financials=(*data.financials,*rows),cashflow_enabled=True)


def cash_card(data=None,as_of=AS_OF):
    result=build_enterprise_analysis(data if data is not None else cash_inputs(),as_of,POLICY)
    assert result['algorithmVersion']=='enterprise-quarter-v3'
    return next(c for c in result['cards'] if c['id']=='lying-flat')


def test_cash_amounts_sum_same_group_before_ratio_not_average_and_legacy_three_unchanged():
    data=cash_inputs(); before=repr(data)
    result=build_enterprise_analysis(data,AS_OF,POLICY)
    assert [c['id'] for c in result['cards']]==['scissors','inventory','white-strip','lying-flat']
    assert result['cards'][:3]==build_enterprise_analysis(replace(data,cashflow_enabled=False),AS_OF,POLICY)['cards']
    c=result['cards'][-1];p=c['points'][-1]
    assert (c['primaryUnit'],c['secondaryUnit'],c['resultUnit'],c['ratioUnit'])==('亿元','亿元','亿元','%')
    assert (p['primary'],p['secondary'],p['result'],p['capitalExpenditureRatio'],p['ratioStatus'])==(10,2,8,20,'ready')
    assert (p['sampleCount'],p['candidateCount'],p['coverage'])==(2,2,100)
    assert repr(data)==before


def test_q2_subtracts_same_year_q1_without_any_prior_year_cash_report():
    data=cash_inputs()
    rows=tuple(FinancialRecord('cashflow',s,date(2024,6,30),date(2024,7,30),None,'1','1','1',
        {'n_cashflow_act':cfo,'c_pay_acq_const_fiolta':capex},f'cash:{s}:2024Q2')
        for s,cfo,capex in zip(data.symbols,(400000000,1800000000),(125000000,375000000)))
    point=cash_card(replace(data,financials=(*data.financials,*rows)),date(2024,8,1))['points'][-1]
    assert (point['primary'],point['secondary'],point['result'],point['capitalExpenditureRatio'])==(12,3,9,25)


@pytest.mark.parametrize('damage',['missing-prior','negative-quarter'])
def test_q2_requires_prior_ytd_and_rejects_reduced_capex_without_abs_or_zero(damage):
    data=cash_inputs()
    rows=[FinancialRecord('cashflow',s,date(2024,6,30),date(2024,7,30),None,'1','1','1',
        {'n_cashflow_act':1000000000,'c_pay_acq_const_fiolta':10000000},f'cash:{s}:2024Q2') for s in data.symbols]
    if damage=='negative-quarter': rows.extend(r for r in data.financials if r.table=='cashflow')
    c=cash_card(replace(data,financials=tuple(rows)),date(2024,8,1))
    assert not c['points'] and c['quality'][-1]['sampleCount']==0
    assert c['quality'][-1]['candidateCount']==2
    assert set(c['quality'][-1]['excluded'].values())=={'missing-report' if damage=='missing-prior' else 'negative-capital-expenditure'}


def test_cashflow_dynamic_sixty_one_members_are_all_counted_without_development_cap():
    from backend.tests.macro.test_enterprise_universe import scaled_inputs
    data=scaled_inputs(61)
    cash=tuple(FinancialRecord('cashflow',s,date(2024,3,31),date(2024,4,30),None,'1','1','1',
        {'n_cashflow_act':100000000,'c_pay_acq_const_fiolta':20000000},f'cash:{s}:2024Q1') for s in data.symbols)
    result=public_enterprise_analysis(replace(data,financials=(*data.financials,*cash),cashflow_enabled=True),AS_OF,POLICY)
    assert valid_enterprise_analysis(result)
    p=result['cards'][-1]['points'][-1]
    assert (p['sampleCount'],p['candidateCount'],p['primary'],p['secondary'],p['result'],p['capitalExpenditureRatio'])==(61,61,61,12.2,48.8,20)


@pytest.mark.parametrize('cfo',[(-100000000,0),(0,0)])
def test_nonpositive_operating_cash_is_kept_but_ratio_is_not_invented(cfo):
    c=cash_card(cash_inputs(cfo=cfo));p=c['points'][-1]
    assert p['primary']==sum(cfo)/100000000 and p['sampleCount']==2
    assert p['capitalExpenditureRatio'] is None and p['ratioStatus']=='non-positive-operating-cashflow'
    assert c['quality'][-1]['status']=='ready'
    assert c['quality'][-1]['negativeIndividualBases']==[]
    assert c['quality'][-1]['zeroIndividualBases']==[]


def test_zero_capex_is_zero_percent_not_missing():
    p=cash_card(cash_inputs(capex=(0,0)))['points'][-1]
    assert p['secondary']==p['capitalExpenditureRatio']==0 and p['ratioStatus']=='ready'


@pytest.mark.parametrize('damage',['missing','null','nan','negative-capex','future','conflict'])
def test_unusable_cash_inputs_stay_in_candidate_denominator(damage):
    data=cash_inputs();rows=list(data.financials)
    original=next(r for r in rows if r.table=='cashflow' and r.symbol==data.symbols[0])
    if damage=='missing': rows.remove(original)
    elif damage=='future': rows[rows.index(original)]=replace(original,ann_date=date(2025,1,1))
    elif damage=='conflict': rows.append(replace(original,values={'n_cashflow_act':999,'c_pay_acq_const_fiolta':1},source_key='conflict'))
    else: rows[rows.index(original)]=replace(original,values={**original.values,'c_pay_acq_const_fiolta':{'null':None,'nan':'NaN','negative-capex':-1}[damage]})
    c=cash_card(replace(data,financials=tuple(rows)))
    assert not c['points']
    q=c['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'],q['coverage'],q['status'])==(1,2,50,'insufficient-coverage')
    assert data.symbols[0] in q['excluded']


def test_cash_card_does_not_depend_on_income_inventory_or_yoy_bases_being_ready():
    data=cash_inputs()
    data=replace(data,financials=tuple(r for r in data.financials if r.table=='cashflow'))
    result=build_enterprise_analysis(data,AS_OF,POLICY)
    assert all(not c['points'] for c in result['cards'][:3])
    assert result['cards'][-1]['points'][-1]['result']==8


@pytest.mark.parametrize('value,status',[('1e-10000','non-finite-aggregate'),('1e10000','non-finite-aggregate')])
def test_nonrepresentable_cash_amounts_do_not_become_zero_or_infinity(value,status):
    c=cash_card(cash_inputs(cfo=(value,0)))
    assert not c['points'] and c['quality'][-1]['status']==status


@pytest.mark.parametrize('cfo,capex',[(('5e-316',0),('8e-316',0)),(('100000000000000000001',0),('100000000000000000000',0))])
def test_lossy_nonzero_float_amounts_cannot_publish_self_contradictory_ready_point(cfo,capex):
    result=public_enterprise_analysis(cash_inputs(cfo=cfo,capex=capex),AS_OF,POLICY)
    c=result['cards'][-1]
    assert not c['points'] and c['quality'][-1]['status']=='non-finite-aggregate'
    assert valid_enterprise_analysis(result)


def test_finite_cash_amounts_with_overflowing_ratio_keep_amount_but_explain_ratio():
    p=cash_card(cash_inputs(cfo=('1e-290',0),capex=('1e300',0)))['points'][-1]
    assert p['primary']>0 and p['secondary']>0
    assert p['capitalExpenditureRatio'] is None and p['ratioStatus']=='non-finite-ratio'


@pytest.mark.parametrize('damage',[None,'unit','ratio-unit','arithmetic','ratio','ratio-reason','missing-card'])
def test_v3_contract_rejects_false_units_amounts_or_ratio_reason(damage):
    result=public_enterprise_analysis(cash_inputs(),AS_OF,POLICY);c=result['cards'][-1];p=c['points'][-1]
    if damage=='unit': c['primaryUnit']='%'
    if damage=='ratio-unit': c['ratioUnit']='亿元'
    if damage=='arithmetic': p['result']=999
    if damage=='ratio': p['capitalExpenditureRatio']=999
    if damage=='ratio-reason': p['ratioStatus']='non-positive-operating-cashflow'
    if damage=='missing-card': result['cards'].pop()
    assert valid_enterprise_analysis(result) is (damage is None)


def test_v3_nonpositive_ratio_contract_accepts_null_but_not_forged_positive_ratio():
    result=public_enterprise_analysis(cash_inputs(cfo=(0,0)),AS_OF,POLICY)
    assert valid_enterprise_analysis(result)
    result['cards'][-1]['points'][-1]['capitalExpenditureRatio']=0
    assert not valid_enterprise_analysis(result)
