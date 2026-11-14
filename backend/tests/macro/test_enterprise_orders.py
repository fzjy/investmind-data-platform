"""订单余额同比与单季营收对照；独立手算、动态同组与缺口。"""
from dataclasses import asdict, replace
from datetime import date

import pytest

from app.macro.enterprise import ReceivableInput, EnterprisePolicy, _hash
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_ledger import ledger_inputs, AS_OF, POLICY


def orders_inputs(current=(100000000,900000000),old=(100000000,300000000)):
    data=ledger_inputs()
    assert hasattr(data,'orders_enabled'), 'order temperature not implemented'
    rows=tuple(replace(r,values={**r.values,'contract_liab':(current if r.end_date.year==2024 else old)[data.symbols.index(r.symbol)]})
        if r.table=='balance' else r for r in data.financials)
    return replace(data,financials=rows,orders_enabled=True)


def result(data=None,as_of=AS_OF,policy=POLICY):
    return public_enterprise_analysis(data if data is not None else orders_inputs(),as_of,policy)


def test_stock_balance_yoy_sum_first_and_old_six_cards_two_nodes_identical():
    data=orders_inputs();v=result(data);old=result(replace(data,orders_enabled=False));m=v['ledger']['metrics'][2];p=m['points'][-1]
    assert v['algorithmVersion']=='enterprise-quarter-v7' and old['algorithmVersion']=='enterprise-quarter-v6'
    assert v['cards']==old['cards'] and v['ledger']['metrics'][:2]==old['ledger']['metrics']
    assert (m['id'],m['unit'],p['value'],p['revenueYoY'],p['orderRevenueSpread'])==('order-temperature','%',150,0,150)
    assert p['amounts']=={'contractLiabilities':10,'baseContractLiabilities':4,'revenue':10,'baseRevenue':10}
    assert valid_enterprise_analysis(v)


def test_q2_balance_not_differenced_but_both_years_revenue_is_quarterly():
    data=orders_inputs();rows=[]
    for r in data.financials:
        if r.table not in ('income','balance'): continue
        fields=({'revenue':int(r.values['revenue'])*(4 if r.end_date.year==2024 else 3)} if r.table=='income'
            else {'contract_liab':200000000 if r.end_date.year==2024 else 100000000})
        rows.append(replace(r,end_date=date(r.end_date.year,6,30),ann_date=date(r.end_date.year,7,30),
            source_key=r.source_key+':q2',values={**r.values,**fields}))
    v=result(replace(data,financials=(*data.financials,*rows)),date(2024,8,1));p=v['ledger']['metrics'][2]['points'][-1]
    assert p['amounts']=={'contractLiabilities':4,'baseContractLiabilities':2,'revenue':30,'baseRevenue':20}
    assert (p['value'],p['revenueYoY'],p['orderRevenueSpread'])==(100,50,50) and valid_enterprise_analysis(v)


@pytest.mark.parametrize('current,old,expected,status', [((0,0),(1,1),-100,'ready'),((0,0),(0,0),None,'zero-contract-liability-base'),((1,1),(0,0),None,'zero-contract-liability-base')])
def test_zero_is_real_balance_but_zero_base_is_not_fake_growth(current,old,expected,status):
    v=result(orders_inputs(current,old));p=v['ledger']['metrics'][2]['points'][-1]
    assert p['value']==expected and p['status']==status and p['revenueYoY']==0
    assert p['orderRevenueSpread']==expected and valid_enterprise_analysis(v)


def test_negative_base_revenue_only_blocks_comparison_not_orders():
    data=orders_inputs();rows=tuple(replace(r,values={**r.values,'revenue':-1}) if r.table=='income' and r.end_date.year==2023 else r for r in data.financials)
    v=result(replace(data,financials=rows));p=v['ledger']['metrics'][2]['points'][-1]
    assert p['value']==150 and p['status']=='ready' and p['revenueYoY'] is None
    assert p['revenueYoYStatus']=='non-positive-aggregate-base-revenue' and p['orderRevenueSpread'] is None
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['negative','null','bool','absent','late','conflict','industry','listing'])
def test_bad_order_input_not_zero_or_removed_from_applicable_denominator(damage):
    data=orders_inputs();s=data.symbols[0];rows=list(data.financials)
    target=next(r for r in rows if r.table=='balance' and r.symbol==s and r.end_date.year==2024)
    if damage=='late': rows[rows.index(target)]=replace(target,f_ann_date=date(2025,1,1))
    elif damage=='conflict': rows.append(replace(target,source_key='conflict',values={**target.values,'contract_liab':123}))
    elif damage=='industry': data=replace(data,industries=tuple(replace(r,effective_from=date(2024,1,1)) if r.symbol==s else r for r in data.industries))
    elif damage=='listing': data=replace(data,securities=tuple(replace(r,list_date=date(2024,4,1)) if r.symbol==s else r for r in data.securities))
    else:
        values=dict(target.values)
        if damage=='absent': values.pop('contract_liab')
        else: values['contract_liab']={'negative':-1,'null':None,'bool':True}[damage]
        rows[rows.index(target)]=replace(target,values=values)
    v=result(replace(data,financials=tuple(rows)),policy=EnterprisePolicy(min_samples=1,min_coverage=80,max_quarters=1));m=v['ledger']['metrics'][2];q=m['quality'][-1]
    if damage=='listing': assert q['sampleCount']==q['candidateCount']==1 and s in q['nonApplicable']
    else:
        assert q['sampleCount']==1 and q['candidateCount']==2 and not m['points']
        assert s in q['excluded']
        if damage=='negative': assert q['excluded'][s]=='invalid-negative-contract-liability'
    assert valid_enterprise_analysis(v)


def test_supplement_source_binding_visibility_and_provenance():
    data=orders_inputs();source=next(r for r in data.financials if r.table=='balance' and r.end_date.year==2024)
    rows=tuple(replace(r,values={k:v for k,v in r.values.items() if k!='contract_liab'}) if r==source else r for r in data.financials)
    source=next(r for r in rows if r.source_key==source.source_key)
    evidence=ReceivableInput(source.source_key,_hash(asdict(source)),'matched','100000000','e'*64,date(2024,5,1),'b'*64)
    data=replace(data,financials=rows,contract_liabilities=(evidence,))
    assert result(data)['ledger']['metrics'][2]['points'][-1]['value']==150
    for broken in (replace(evidence,source_hash='0'*64),replace(evidence,available_date=date(2024,6,1)),replace(evidence,status='returned-null',value=None)):
        v=result(replace(data,contract_liabilities=(broken,)),policy=EnterprisePolicy(min_samples=2,max_quarters=1))
        assert not v['ledger']['metrics'][2]['points'] and valid_enterprise_analysis(v)
    with pytest.raises(ValueError): result(replace(data,contract_liabilities=(evidence,evidence)))


@pytest.mark.parametrize('damage',['value','comparison','spread','status','unit','amount-negative','missing-key','members','wrong-version'])
def test_order_contract_rejects_inconsistent_values(damage):
    v=result();m=v['ledger']['metrics'][2];p=m['points'][-1]
    if damage=='value': p['value']=1
    elif damage=='comparison': p['revenueYoY']=2
    elif damage=='spread': p['orderRevenueSpread']=0
    elif damage=='status': p.update(value=None,status='zero-contract-liability-base')
    elif damage=='unit': m['unit']='百分点'
    elif damage=='amount-negative': p['amounts']['contractLiabilities']=-1
    elif damage=='missing-key': p['amounts'].pop('baseRevenue')
    elif damage=='members': m['quality'][-1]['included'].append('outside')
    else: v['algorithmVersion']='enterprise-quarter-v6'
    assert not valid_enterprise_analysis(v)


@pytest.mark.parametrize('flag',[1,None,'yes'])
def test_orders_switch_is_strict_bool_and_requires_ledger(flag):
    with pytest.raises(ValueError): result(replace(orders_inputs(),orders_enabled=flag))


@pytest.mark.parametrize('raw',['1e-10000','1e10000'])
def test_unrepresentable_order_amounts_never_emit_fake_zero(raw):
    v=result(orders_inputs(current=(raw,0)));m=v['ledger']['metrics'][2]
    assert not m['points'] and m['quality'][-1]['status']=='non-finite-aggregate' and valid_enterprise_analysis(v)


def test_dynamic_three_stock_pool_and_financial_exemption_without_fixed_count():
    data=orders_inputs();source=data.symbols[0];third='000003.SZ'
    data=replace(data,symbols=(*data.symbols,third),
        securities=(*data.securities,replace(data.securities[0],symbol=third)),
        industries=(*data.industries,replace(data.industries[0],symbol=third,root_code='801790.SI')),
        financials=(*data.financials,*(replace(r,symbol=third,source_key=r.source_key+':third') for r in data.financials if r.symbol==source)))
    v=result(data);m=v['ledger']['metrics'][2];q=m['quality'][-1]
    assert v['poolCount']==3 and q['candidateCount']==q['sampleCount']==2
    assert q['nonApplicable']=={third:'financial-industry-review'} and m['points'][-1]['value']==150
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('current,base,want,status',[(1.0000000000000002,1,2e-14,'ready'),(1e-300,1e300,-100,'ready'),(1e300,1e-300,None,'non-finite-ratio'),(-1e308,1e308,-200,'ready')])
def test_order_growth_handles_cancellation_and_intermediate_overflow(current,base,want,status):
    from app.macro.enterprise_orders import growth
    value,reason=growth(current,base,'base-invalid')
    assert reason==status
    if want is None: assert value is None
    else: assert value==pytest.approx(want,rel=1e-10,abs=0)


def test_nearly_equal_growth_spread_uses_exact_public_decimal_difference_not_rounded_yoys():
    from app.macro.enterprise_orders import order_values
    v=order_values({'contractLiabilities':10,'baseContractLiabilities':9,'revenue':10.00000001,'baseRevenue':9.00000001})
    # (10/9 - 10.00000001/9.00000001)*100，独立精确分数100/8100000009。
    assert v['orderRevenueSpread']==pytest.approx(100/8100000009,rel=1e-15,abs=0)
