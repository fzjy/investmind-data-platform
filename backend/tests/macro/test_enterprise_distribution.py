"""分红/利润分配/偿息是合并现金支付，严格同组单季金额与同比。"""
from dataclasses import replace
from datetime import date

import pytest

from app.macro.enterprise import FinancialRecord, build_enterprise_analysis
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_cashflow import AS_OF, POLICY, cash_inputs

FIELD='c_pay_dist_dpcp_int_exp'


def distribution_inputs(current=(100000000,900000000),old=(100000000,300000000)):
    data=cash_inputs()
    rows=tuple(replace(r,values={**r.values,FIELD:current[data.symbols.index(r.symbol)]}) if r.table=='cashflow' else r for r in data.financials)
    prior=tuple(FinancialRecord('cashflow',s,date(2023,3,31),date(2023,4,30),None,'1','1','1',
        {FIELD:old[i]},f'distribution:{s}:2023Q1') for i,s in enumerate(data.symbols))
    return replace(data,financials=(*rows,*prior),distribution_enabled=True)


def result(data=None,as_of=AS_OF):
    return public_enterprise_analysis(data if data is not None else distribution_inputs(),as_of,POLICY)


def test_same_group_amounts_and_yoy_not_average_with_original_four_exactly_unchanged():
    data=distribution_inputs();before=repr(data); value=result(data)
    assert value['algorithmVersion']=='enterprise-quarter-v4'
    assert [c['id'] for c in value['cards']]==['scissors','inventory','white-strip','lying-flat','dividend']
    assert value['cards'][:4]==result(replace(data,distribution_enabled=False))['cards']
    c=value['cards'][-1];p=c['points'][-1]
    assert (c['primaryUnit'],c['secondaryUnit'],c['resultUnit'],c['yoyUnit'])==('亿元','亿元','亿元','%')
    assert (p['primary'],p['secondary'],p['result'],p['distributionBase'],p['distributionYoY'],p['distributionYoYStatus'])==(2,10,-8,4,150,'ready')
    assert (p['sampleCount'],p['candidateCount'],p['coverage'])==(2,2,100)
    assert valid_enterprise_analysis(value) and repr(data)==before


def test_q2_subtracts_both_years_q1_but_does_not_require_old_capex():
    data=distribution_inputs();rows=[]
    for year,payments in ((2023,(300000000,900000000)),(2024,(400000000,1800000000))):
        for i,s in enumerate(data.symbols):
            rows.append(FinancialRecord('cashflow',s,date(year,6,30),date(year,7,30),None,'1','1','1',
                {FIELD:payments[i],**({'c_pay_acq_const_fiolta':(125000000,375000000)[i]} if year==2024 else {})},f'pay:{s}:{year}Q2'))
    p=result(replace(data,financials=(*data.financials,*rows)),date(2024,8,1))['cards'][-1]['points'][-1]
    assert (p['primary'],p['secondary'],p['result'],p['distributionBase'],p['distributionYoY'])==(3,12,-9,8,50)


@pytest.mark.parametrize('current,old,want,status',[
    ((0,0),(100000000,300000000),-100,'ready'),
    ((100000000,900000000),(0,0),None,'zero-distribution-base'),
    ((0,0),(0,0),None,'zero-distribution-base'),
])
def test_zero_payments_and_zero_base_keep_actual_amounts(current,old,want,status):
    value=result(distribution_inputs(current,old));c=value['cards'][-1];p=c['points'][-1]
    assert p['secondary']==sum(current)/100000000 and p['sampleCount']==2
    assert p['distributionYoY']==want and p['distributionYoYStatus']==status
    assert c['quality'][-1]['zeroIndividualBases']==(list(value['symbols']) if old==(0,0) else [])
    assert valid_enterprise_analysis(value)


@pytest.mark.parametrize('damage',['missing-current','missing-base','null','nan','negative-current','negative-base','future','conflict','listing','industry'])
def test_missing_or_incompatible_inputs_stay_in_candidate_denominator(damage):
    data=distribution_inputs();symbol=data.symbols[0];rows=list(data.financials)
    target=next(r for r in rows if r.table=='cashflow' and r.symbol==symbol and r.end_date.year==(2023 if damage in ('missing-base','negative-base') else 2024))
    if damage.startswith('missing-'): rows.remove(target)
    elif damage=='future': rows[rows.index(target)]=replace(target,ann_date=date(2025,1,1))
    elif damage=='conflict': rows.append(replace(target,source_key='conflict',values={**target.values,FIELD:999}))
    elif damage=='listing': data=replace(data,securities=tuple(replace(s,list_date=date(2024,1,1)) if s.symbol==symbol else s for s in data.securities))
    elif damage=='industry': data=replace(data,industries=tuple(replace(i,effective_from=date(2024,1,1)) if i.symbol==symbol else i for i in data.industries))
    else: rows[rows.index(target)]=replace(target,values={**target.values,FIELD:None if damage=='null' else 'NaN' if damage=='nan' else -1})
    value=result(replace(data,financials=tuple(rows))); c=value['cards'][-1]
    assert not c['points']
    q=c['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'],q['coverage'],q['status'])==(1,2,50,'insufficient-coverage')
    assert symbol in q['excluded'] and valid_enterprise_analysis(value)


def test_negative_quarter_payment_is_not_abs_or_zero():
    data=distribution_inputs(); rows=[]
    for r in data.financials:
        if r.table=='cashflow' and r.end_date.year==2024:
            rows.append(replace(r,end_date=date(2024,6,30),ann_date=date(2024,7,31),source_key=r.source_key+':q2',
                values={**r.values,FIELD:0,'c_pay_acq_const_fiolta':500000000}))
    c=result(replace(data,financials=(*data.financials,*rows)),date(2024,8,1))['cards'][-1]
    assert not c['points'] and set(c['quality'][-1]['excluded'].values())=={'negative-distribution-payment'}


@pytest.mark.parametrize('current,old',[(('1e10000',0),(1,1)),(('5e-316',0),('8e-316',0)),((1,1),('1e-10000',0))])
def test_unrepresentable_amount_or_base_does_not_create_false_ready_dto(current,old):
    value=result(distribution_inputs(current,old));c=value['cards'][-1]
    assert not c['points'] and c['quality'][-1]['status']=='non-finite-aggregate'
    assert valid_enterprise_analysis(value)


def test_overflowing_yoy_keeps_finite_amounts_and_explains_missing_yoy():
    value=result(distribution_inputs(('1e300',0),('1e-290',0)));p=value['cards'][-1]['points'][-1]
    assert p['secondary']>0 and p['distributionBase']>0
    assert p['distributionYoY'] is None and p['distributionYoYStatus']=='non-finite-yoy'
    assert valid_enterprise_analysis(value)


def test_combined_field_does_not_add_minority_dividend_or_debt_principal_again():
    data=distribution_inputs();rows=tuple(replace(r,values={**r.values,'incl_dvd_profit_paid_sc_ms':999999999,'c_prepay_amt_borr':999999999}) if r.table=='cashflow' else r for r in data.financials)
    p=result(replace(data,financials=rows))['cards'][-1]['points'][-1]
    assert (p['secondary'],p['distributionBase'],p['distributionYoY'])==(10,4,150)


@pytest.mark.parametrize('damage',[None,'unit','yoy-unit','base','yoy','reason','missing-card'])
def test_contract_rejects_inconsistent_v4_amounts_base_yoy_and_status(damage):
    value=result();c=value['cards'][-1];p=c['points'][-1]
    if damage=='unit': c['primaryUnit']='%'
    if damage=='yoy-unit': c['yoyUnit']='亿元'
    if damage=='base': p['distributionBase']=-1
    if damage=='yoy': p['distributionYoY']=999
    if damage=='reason': p['distributionYoYStatus']='zero-distribution-base'
    if damage=='missing-card': value['cards'].pop()
    assert valid_enterprise_analysis(value) is (damage is None)


@pytest.mark.parametrize('flag',[1,None,'yes'])
def test_distribution_switch_cannot_be_truthy_non_bool(flag):
    with pytest.raises(ValueError): result(replace(distribution_inputs(),distribution_enabled=flag))


def test_distribution_requires_cashflow_contract_and_never_changes_default_hash():
    data=distribution_inputs()
    with pytest.raises(ValueError): result(replace(data,cashflow_enabled=False))
    disabled=replace(data,distribution_enabled=False)
    assert build_enterprise_analysis(disabled,AS_OF,POLICY)['algorithmVersion']=='enterprise-quarter-v3'


def test_sixty_one_members_are_dynamic_without_development_pool_cap():
    from backend.tests.macro.test_enterprise_universe import scaled_inputs
    data=scaled_inputs(61);rows=[]
    for s in data.symbols:
        for year,payment in ((2023,100000000),(2024,200000000)):
            rows.append(FinancialRecord('cashflow',s,date(year,3,31),date(year,4,30),None,'1','1','1',
                {FIELD:payment,'n_cashflow_act':300000000,'c_pay_acq_const_fiolta':100000000},f'{s}:{year}'))
    value=result(replace(data,financials=(*data.financials,*rows),cashflow_enabled=True,distribution_enabled=True))
    p=value['cards'][-1]['points'][-1]
    assert (p['sampleCount'],p['candidateCount'],p['primary'],p['secondary'],p['distributionBase'],p['distributionYoY'])==(61,61,61,122,61,100)
    assert valid_enterprise_analysis(value)
