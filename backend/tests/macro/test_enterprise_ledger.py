"""账本两指标：独立手算同组金额、保留符号、不可解释比率不冒充0。"""
from dataclasses import replace
from datetime import date

import pytest

from app.macro.enterprise import EnterprisePolicy, FinancialRecord
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_loss import loss_inputs, AS_OF, POLICY


def ledger_inputs(profit=(-100000000,300000000),revenue=(100000000,900000000),cost=(50000000,450000000)):
    data=loss_inputs()
    assert hasattr(data,'ledger_enabled'), 'ledger not implemented'
    rows=[]
    for r in data.financials:
        if r.table=='income':
            i=data.symbols.index(r.symbol)
            fields=({'revenue':revenue[i],'oper_cost':cost[i],'n_income':profit[i]} if r.end_date.year==2024
                else {'revenue':(100000000,900000000)[i],'oper_cost':(80000000,720000000)[i]})
            r=replace(r,values={**r.values,**fields})
        rows.append(r)
    return replace(data,financials=tuple(rows),ledger_enabled=True)


def result(data=None,as_of=AS_OF,policy=POLICY):
    return public_enterprise_analysis(ledger_inputs() if data is None else data,as_of,policy)


def test_sum_before_margin_and_ratio_uses_consolidated_not_parent_profit_and_keeps_six_cards():
    data=ledger_inputs();v=result(data);old=result(replace(data,ledger_enabled=False))
    assert v['algorithmVersion']=='enterprise-quarter-v6' and old['algorithmVersion']=='enterprise-quarter-v5'
    assert v['cards']==old['cards'] and v['inputHash']!=old['inputHash'] and 'ledger' not in old
    pricing,cash=v['ledger']['metrics'];p=pricing['points'][-1];c=cash['points'][-1]
    assert (pricing['id'],pricing['unit'])==('pricing-power','百分点') and p['value']==pytest.approx(30)
    assert p['amounts']=={'revenue':10,'cost':5,'baseRevenue':10,'baseCost':8}
    assert (cash['id'],cash['unit'],c['value'])==('cash-conversion','倍',5)
    assert c['amounts']=={'operatingCashflow':10,'netIncome':2}
    assert cash['quality'][-1]['negativeIndividualBases']==[data.symbols[0]]
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('profit',[(-1,1),(-3,-1)])
def test_non_positive_aggregate_profit_keeps_loss_members_and_amounts_but_null_ratio(profit):
    v=result(ledger_inputs(profit=profit));m=v['ledger']['metrics'][1];p=m['points'][-1]
    assert p['value'] is None and p['status']=='non-positive-aggregate-profit'
    assert p['sampleCount']==2 and p['amounts']['netIncome']==sum(profit)/1e8
    assert m['quality'][-1]['status']=='ready' and valid_enterprise_analysis(v)


def test_negative_quarter_cost_and_negative_revenue_are_preserved_not_abs_or_filtered():
    data=ledger_inputs(revenue=(-100000000,900000000),cost=(-50000000,450000000))
    v=result(data);m=v['ledger']['metrics'][0];p=m['points'][-1]
    assert p['amounts']['revenue']==8 and p['amounts']['cost']==4 and p['value']==pytest.approx(30)
    assert m['quality'][-1]['negativeQuarterCosts']==[data.symbols[0]]
    assert m['quality'][-1]['negativeIndividualBases']==[data.symbols[0]] and valid_enterprise_analysis(v)


@pytest.mark.parametrize('revenue',[(0,0),(-1,0)])
def test_nonpositive_revenue_preserves_observation_without_margin(revenue):
    v=result(ledger_inputs(revenue=revenue));p=v['ledger']['metrics'][0]['points'][-1]
    assert p['status']=='non-positive-aggregate-revenue' and p['value'] is None
    assert valid_enterprise_analysis(v)


def test_quarter_ytd_difference_uses_both_years_same_group():
    data=ledger_inputs();rows=[]
    for r in data.financials:
        if r.table=='income':
            fields=({'revenue':int(r.values['revenue'])*3,'oper_cost':int(r.values['oper_cost'])*2,'n_income':400000000}
                if r.end_date.year==2024 else {'revenue':int(r.values['revenue'])*3,'oper_cost':int(r.values['oper_cost'])*3})
        elif r.table=='cashflow': fields={'n_cashflow_act':2000000000}
        else: continue
        rows.append(replace(r,end_date=date(r.end_date.year,6,30),ann_date=date(r.end_date.year,7,30),source_key=r.source_key+':q2',values={**r.values,**fields}))
    v=result(replace(data,financials=(*data.financials,*rows)),date(2024,8,1));a,b=v['ledger']['metrics']
    assert a['points'][-1]['amounts']=={'revenue':20,'cost':5,'baseRevenue':20,'baseCost':16}
    assert a['points'][-1]['value']==pytest.approx(55)
    assert b['points'][-1]['amounts']=={'operatingCashflow':30,'netIncome':6}
    assert b['points'][-1]['value']==5 and valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['missing-profit','missing-cost','late','conflict','industry','listing','type-conflict','bool'])
def test_incompatible_or_missing_inputs_stay_in_dynamic_candidate_denominator(damage):
    data=ledger_inputs();s=data.symbols[0];rows=list(data.financials)
    target=next(r for r in rows if r.table=='income' and r.symbol==s and r.end_date.year==2024)
    metric=0 if damage=='missing-cost' else 1
    if damage=='late': rows[rows.index(target)]=replace(target,f_ann_date=date(2025,1,1))
    elif damage=='conflict': rows.append(replace(target,source_key='conflict',values={**target.values,'n_income':123}))
    elif damage=='industry': data=replace(data,industries=tuple(replace(r,effective_from=date(2024,1,1)) if r.symbol==s else r for r in data.industries));metric=0
    elif damage=='listing': data=replace(data,securities=tuple(replace(r,list_date=date(2024,4,1)) if r.symbol==s else r for r in data.securities));metric=0
    elif damage=='type-conflict':
        cash=next(r for r in rows if r.table=='cashflow' and r.symbol==s and r.end_date.year==2024)
        rows[rows.index(cash)]=replace(cash,comp_type='2')
    else:
        fields=dict(target.values);field='oper_cost' if damage=='missing-cost' else 'n_income'
        if damage=='bool': fields[field]=True
        else: fields.pop(field)
        rows[rows.index(target)]=replace(target,values=fields)
    v=result(replace(data,financials=tuple(rows)));m=v['ledger']['metrics'][metric];q=m['quality'][-1]
    if damage=='listing':
        assert q['candidateCount']==q['sampleCount']==1 and q['nonApplicable']=={s:'before-listing'}
    else:
        assert (q['candidateCount'],q['sampleCount'],q['coverage'],q['status'])==(2,1,50,'insufficient-coverage')
        assert not m['points'] and s in q['excluded']
    assert valid_enterprise_analysis(v)


def test_financial_industry_is_not_zero_or_missing_and_latest_quarter_stays_missing():
    data=ledger_inputs();s=data.symbols[0]
    data=replace(data,industries=tuple(replace(r,root_code='801790.SI') if r.symbol==s else r for r in data.industries))
    v=result(data,date(2024,8,1),EnterprisePolicy(min_samples=1,max_quarters=2));m=v['ledger']['metrics'][1]
    assert m['points'][0]['sampleCount']==m['points'][0]['candidateCount']==1
    assert m['quality'][-1]['period']=='2024-06-30' and m['quality'][-1]['status']=='insufficient-samples'
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('raw',['1e-10000','1e10000'])
def test_unrepresentable_amounts_never_emit_fake_zero(raw):
    v=result(ledger_inputs(profit=(raw,0)));m=v['ledger']['metrics'][1]
    assert not m['points'] and m['quality'][-1]['status']=='non-finite-aggregate'
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['value','null-reason','unit','amount','member','point-count','sign-list','version-missing-ledger'])
def test_contract_rejects_inconsistent_ledger_evidence(damage):
    v=result();m=v['ledger']['metrics'][1];p=m['points'][-1];q=m['quality'][-1]
    if damage=='value': p['value']=99
    elif damage=='null-reason': p.update(value=None,status='non-positive-aggregate-profit')
    elif damage=='unit': m['unit']='%'
    elif damage=='amount': p['amounts']['netIncome']=True
    elif damage=='member': q['included'].append('outside')
    elif damage=='point-count': m['points']=[]
    elif damage=='sign-list': q['negativeQuarterCosts']=['outside']
    else: v.pop('ledger')
    assert not valid_enterprise_analysis(v)


@pytest.mark.parametrize('flag',[1,None,'yes'])
def test_switch_is_not_truthy_coercion(flag):
    with pytest.raises(ValueError): result(replace(ledger_inputs(),ledger_enabled=flag))


def test_sixty_one_scope_is_not_capped_and_negative_cashflow_stays_negative():
    data=ledger_inputs();template=data.symbols[1];symbols=tuple(f'{i:06}.SZ' for i in range(61))
    data=replace(data,symbols=symbols,securities=tuple(replace(data.securities[1],symbol=s) for s in symbols),
        industries=tuple(replace(data.industries[1],symbol=s) for s in symbols),
        financials=tuple(replace(r,symbol=s,source_key=f'{r.source_key}:{s}',values={**r.values,**({'n_cashflow_act':-300000000} if r.table=='cashflow' else {})}) for s in symbols for r in data.financials if r.symbol==template),
        market_values=(),trading_days=())
    v=result(data);p=v['ledger']['metrics'][1]['points'][-1]
    assert (p['sampleCount'],p['candidateCount'],p['coverage'],p['value'])==(61,61,100,-1)
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('profit,cfo,status',[(('1e-300',0),(100000000,900000000),'non-finite-ratio'),(('1e308',0),('1e-290',0),'non-finite-ratio'),((1,1),(0,0),'ready')])
def test_ratio_overflow_underflow_and_real_zero(profit,cfo,status):
    data=ledger_inputs(profit=profit)
    rows=tuple(replace(r,values={**r.values,'n_cashflow_act':cfo[data.symbols.index(r.symbol)]}) if r.table=='cashflow' else r for r in data.financials)
    v=result(replace(data,financials=rows));p=v['ledger']['metrics'][1]['points'][-1]
    assert p['status']==status and p['value']==(0 if status=='ready' else None)
    assert valid_enterprise_analysis(v)


def test_q4_annual_minus_q3_not_annual_value_and_prior_revision_after_cutoff_is_invisible():
    data=ledger_inputs();rows=[]
    for year in (2023,2024):
        for month,day,multiplier in ((9,30,3),(12,31,5)):
            for r in data.financials:
                if r.end_date.year!=year or r.table not in ('income','cashflow'): continue
                fields={k:str(float(v)*multiplier) for k,v in r.values.items()}
                rows.append(replace(r,end_date=date(year,month,day),ann_date=date(year+1,1,30),source_key=r.source_key+f':{month}',values=fields))
    data=replace(data,financials=tuple(rows));v=result(data,date(2025,2,1));a,b=v['ledger']['metrics']
    assert a['points'][-1]['amounts']=={'revenue':20,'cost':10,'baseRevenue':20,'baseCost':16}
    assert b['points'][-1]['amounts']=={'operatingCashflow':20,'netIncome':4}
    late=replace(rows[0],f_ann_date=date(2025,2,2),values={'revenue':9999},source_key='future')
    assert result(replace(data,financials=(*rows,late)),date(2025,2,1))['ledger']==v['ledger']
    assert valid_enterprise_analysis(v)


def test_ratio_cancellation_does_not_publish_false_zero_margin():
    from app.macro.enterprise_ledger import ledger_value
    assert ledger_value('pricing-power',{'revenue':1,'cost':1e-300,'baseRevenue':1,'baseCost':2e-300})==(None,'non-finite-ratio')


def test_exact_equal_decimal_margin_is_positive_zero_without_rejecting_legacy_snapshot():
    from app.macro.enterprise_ledger import ledger_value
    amounts={'revenue':0.3,'cost':0.1,'baseRevenue':0.9,'baseCost':0.3}
    assert ledger_value('pricing-power',amounts)==(0,'ready')
    v=result()
    assert v['ledger']['calculationVersion']=='exact-zero-margin-v1'
    p=v['ledger']['metrics'][0]['points'][0]
    p.update(amounts=amounts,value=0,status='ready')
    assert valid_enterprise_analysis(v)
    p['value']=-1.4210854715202004e-14
    assert not valid_enterprise_analysis(v)
    v['ledger'].pop('calculationVersion')
    assert valid_enterprise_analysis(v)
    v['ledger']['calculationVersion']='unknown'
    assert not valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['all-negative-positive','all-zero-positive','overlap','no-negative-negative','no-zero-zero','negative-cost-unlisted','negative-revenue-unlisted'])
def test_contract_rejects_impossible_member_signs_and_aggregate_amounts(damage):
    from app.macro.enterprise_ledger import ledger_value
    v=result();m=v['ledger']['metrics'][0 if damage.startswith('negative-') else 1];q=m['quality'][0];p=m['points'][0]
    if damage=='all-negative-positive': q['negativeIndividualBases']=q['included'][:]
    elif damage=='all-zero-positive': q['zeroIndividualBases']=q['included'][:]
    elif damage=='overlap': q['zeroIndividualBases']=q['negativeIndividualBases'][:]
    elif damage=='no-negative-negative': q['negativeIndividualBases']=[];p['amounts']['netIncome']=-2
    elif damage=='no-zero-zero': q['negativeIndividualBases']=[];p['amounts']['netIncome']=0
    elif damage=='negative-cost-unlisted': p['amounts']['cost']=-5
    else: p['amounts']['revenue']=-10
    p['value'],p['status']=ledger_value(m['id'],p['amounts'])
    assert not valid_enterprise_analysis(v)
