"""REA口径反例：改成金额、不同组同比、补零或反方向必须失败。"""
from dataclasses import replace
from datetime import date, timedelta
from copy import deepcopy
import importlib
import importlib.util

import pytest

from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, FinancialRecord, SecurityInput, IndustryInput


def implementation():
    assert importlib.util.find_spec('app.macro.enterprise_rea') is not None, 'REA projection is missing'
    return importlib.import_module('app.macro.enterprise_rea')


def controlled(count=2):
    symbols=tuple(f'{i:06d}.SZ' for i in range(count))
    rows=[]
    for symbol in symbols:
        for year,factor in ((2023,1),(2024,2)):
            for month,multiplier in ((3,1),(6,3)):
                period=date(year,month,31 if month==3 else 30)
                values={
                    'income':{'revenue':100*factor*multiplier,'oper_cost':60*factor*multiplier,'n_income':10*factor*multiplier},
                    'balance':{'contract_liab':20*factor,'fix_assets':50*factor,'acct_payable':30*factor,'money_cap':40*factor,
                        'st_borr':5*factor,'lt_borr':5*factor,'bond_payable':5*factor,'non_cur_liab_due_1y':5*factor},
                    'cashflow':{'n_cashflow_act':30*factor*multiplier,'c_pay_acq_const_fiolta':10*multiplier,
                        'c_pay_dist_dpcp_int_exp':5*factor*multiplier,'c_paid_to_for_empl':10*factor*multiplier,
                        'depr_fa_coga_dpba':3*multiplier,'amort_intang_assets':1*multiplier,'lt_amort_deferred_exp':1*multiplier},
                }
                for table,fields in values.items():
                    rows.append(FinancialRecord(table,symbol,period,period+timedelta(days=10),None,'1','1','1',fields,f'{table}:{symbol}:{period}'))
    return EnterpriseInputs('controlled',symbols,tuple(SecurityInput(s,date(2020,1,1)) for s in symbols),tuple(rows),
        tuple(IndustryInput(s,'801120.SI',date(2020,1,1),None) for s in symbols))


def build(data=None):
    data=data or controlled()
    return implementation().build_rea_projection(data,date(2024,8,1),EnterprisePolicy(min_samples=1),{'inputHash':'a'*64})


def metric(result,identifier):
    return next(m for m in result['series'] if m['id']==identifier)


def test_rea_uses_yoy_not_amount_and_q2_is_single_quarter():
    result=build()
    for identifier in ('lying-flat','dividend'):
        m=metric(result,identifier); p=m['points'][-1]
        assert p['period']=='2024-06-30'
        assert (p['primary'],p['secondary'],p['value'],p['spread'])==(0,100,100,-100)
    p=metric(result,'lying-flat')['points'][-1]
    assert p['amounts']['a']==40  # 两股单季Capex：(30−10)×2，非H1累计60。
    assert p['ratio']==pytest.approx(1/6)


def test_all_eight_metrics_and_dynamic_universe():
    result=build(controlled(61))
    assert len(result['series'])==10
    assert result['poolCount']==61
    for m in result['series']:
        assert m['points'][-1]['sampleCount']==61
        assert m['quality'][-1]['candidateCount']==61
    assert metric(result,'pricing')['points'][-1]['value']==0
    assert metric(result,'cash')['points'][-1]['value']==3
    assert metric(result,'renewal')['points'][-1]['value']==2


def test_missing_component_not_zero_and_missing_member_stays_denominator():
    data=controlled(); data=replace(data,financials=tuple(replace(r,values={k:v for k,v in r.values.items() if k!='bond_payable'})
        if r.symbol==data.symbols[0] and r.table=='balance' else r for r in data.financials))
    m=metric(build(data),'debt')
    assert m['points']==[]
    assert m['quality'][-1]['candidateCount']==2
    assert m['quality'][-1]['sampleCount']==1
    assert m['quality'][-1]['excluded'][data.symbols[0]].startswith('missing-field:')


def test_zero_base_keeps_observation_but_null_yoy():
    data=controlled(); data=replace(data,financials=tuple(replace(r,values={**r.values,'n_cashflow_act':0})
        if r.table=='cashflow' and r.end_date.year==2023 else r for r in data.financials))
    p=metric(build(data),'lying-flat')['points'][-1]
    assert p['secondary'] is None and p['value'] is None
    assert p['ratio']==pytest.approx(1/6)
    assert p['status']=='non-positive-yoy-base'


def test_precise_near_equal_spread_is_not_cancelled_to_zero():
    value=implementation().rea_values('debt',{'a':100000000000000000001,'b':100000000000000000002,
        'oldA':90000000000000000001,'oldB':90000000000000000002})
    assert value['spread'] != 0


def test_tiny_spread_does_not_cancel_at_decimal_context_precision():
    p=implementation().rea_values('debt',{'a':1e-100,'b':2e-100,'oldA':1,'oldB':1})
    assert p['value']==pytest.approx(-1e-98,abs=0,rel=1e-12)


def test_build_refuses_float_amounts_that_erase_nonzero_source_growth():
    data=controlled()
    data=replace(data,financials=tuple(replace(r,values={**r.values,'contract_liab':
        '10000000000000000.01' if r.end_date.year==2024 else '10000000000000000'}) if r.table=='balance' else r for r in data.financials))
    m=metric(build(data),'order')
    assert m['points']==[]
    assert m['quality'][-1]['status']=='non-finite-aggregate'


def test_negative_single_quarter_amortization_is_not_hidden_by_positive_total():
    data=controlled()
    data=replace(data,financials=tuple(replace(r,values={**r.values,'amort_intang_assets':0})
        if r.table=='cashflow' and r.end_date==date(2024,6,30) else r for r in data.financials))
    m=metric(build(data),'renewal')
    assert not any(p['period']=='2024-06-30' for p in m['points'])
    assert all(why.startswith('negative-quarter') for why in m['quality'][-1]['excluded'].values())


@pytest.mark.parametrize('damage',['negative-recomputed','tiny-zero'])
def test_coordinated_amount_or_zero_damage_rejected(damage):
    result=build(); parent={'inputHash':'a'*64,'poolCount':2,'symbols':list(controlled().symbols),'asOfDate':'2024-08-01',
        'policy':{'min_samples':1,'min_coverage':80,'max_quarters':40}}
    m=metric(result,'renewal' if damage=='negative-recomputed' else 'debt');p=m['points'][-1]
    p['amounts']={'a':-20,'b':20,'oldA':10,'oldB':10} if damage=='negative-recomputed' else {'a':100000000001,'b':100000000002,'oldA':90000000001,'oldB':90000000002}
    p.update(implementation().rea_values(m['id'],p['amounts']))
    if damage=='tiny-zero': p['value']=p['spread']=0
    assert not implementation().valid_rea_projection(result,parent)


def test_missing_base_member_never_uses_another_quarters_comparable_group():
    data=controlled()
    data=replace(data,financials=tuple(r for r in data.financials if not (r.symbol==data.symbols[0]
        and r.table=='cashflow' and r.end_date==date(2023,3,31))))
    m=metric(build(data),'lying-flat')
    assert m['points']==[]
    assert m['quality'][-1]['sampleCount']==1


def test_later_revision_and_mother_company_report_do_not_replace_visible_consolidated():
    data=controlled(); row=next(r for r in data.financials if r.table=='cashflow' and r.end_date==date(2024,6,30))
    later=replace(row,ann_date=date(2025,1,1),values={**row.values,'n_cashflow_act':9999},source_key='later')
    mother=replace(row,report_type='6',values={**row.values,'n_cashflow_act':9999},source_key='mother')
    assert build(data)==build(replace(data,financials=(*data.financials,later,mother)))


@pytest.mark.parametrize('damage',['value','amount','member','coverage','unit','period','hash'])
def test_corrupt_projection_rejected(damage):
    result=build(); parent={'inputHash':'a'*64,'poolCount':2,'symbols':list(controlled().symbols),'asOfDate':'2024-08-01',
        'policy':{'min_samples':1,'min_coverage':80,'max_quarters':40}}
    assert implementation().valid_rea_projection(result,parent)
    bad=deepcopy(result); m=metric(bad,'lying-flat')
    if damage=='value': m['points'][-1]['value']=-999
    if damage=='amount': m['points'][-1]['amounts']['a']=float('inf')
    if damage=='member': m['quality'][-1]['included']=['outside']
    if damage=='coverage': m['quality'][-1]['coverage']=0
    if damage=='unit': m['unit']='亿元'
    if damage=='period': m['points'][-1]['period']='2024-02-31'
    if damage=='hash': bad['sourceHash']='b'*64
    assert not implementation().valid_rea_projection(bad,parent)
