"""固定估值日的同组市值权重；缺失不等于未亏损，后修订不倒灌。"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.macro import enterprise
from app.macro.enterprise import EnterprisePolicy, FinancialRecord
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_distribution import distribution_inputs

AS_OF=date(2024,5,31)
POLICY=EnterprisePolicy(min_samples=1,max_quarters=1)


def loss_inputs(current=(-1,1),old=(-2,2),weights=(10000,90000)):
    assert hasattr(enterprise,'MarketValueInput'), 'loss inputs not implemented'
    data=distribution_inputs()
    rows=tuple(replace(r,values={**r.values,'n_income_attr_p':(current if r.end_date.year==2024 else old)[data.symbols.index(r.symbol)]})
        if r.table=='income' else r for r in data.financials)
    days=tuple(enterprise.TradingDayInput(date(2024,5,d),d==6,f'calendar:{d}') for d in range(1,7))
    caps=tuple(enterprise.MarketValueInput(s,date(2024,5,6),weights[i],f'market:{s}') for i,s in enumerate(data.symbols))
    return replace(data,financials=rows,loss_enabled=True,trading_days=days,market_values=caps)


def result(data=None,as_of=AS_OF,policy=POLICY):
    return public_enterprise_analysis(loss_inputs() if data is None else data,as_of,policy)


@pytest.mark.parametrize('target',['asOfDate','valuationDate'])
def test_noncanonical_compact_dates_are_rejected_like_typescript(target):
    value=result()
    if target=='asOfDate':
        value['asOfDate']='20240531'
    else:
        value['cards'][-1]['quality'][-1]['valuationDate']='20240506'
        value['cards'][-1]['points'][-1]['valuationDate']='20240506'
    assert not valid_enterprise_analysis(value)


def test_weighted_loss_and_decline_not_count_share_or_average_and_original_five_unchanged():
    data=loss_inputs();before=repr(data);v=result(data);c=v['cards'][-1];p=c['points'][-1];q=c['quality'][-1]
    assert v['algorithmVersion']=='enterprise-quarter-v5' and len(v['cards'])==6
    assert v['cards'][:5]==result(replace(data,loss_enabled=False))['cards']
    assert (c['id'],c['primaryUnit'],c['secondaryUnit'],c['resultUnit'],c['marketValueUnit'])==('loss','%','%','百分点','亿元')
    assert (p['primary'],p['secondary'],p['result'])==(10,90,-80)
    assert (p['totalMarketValue'],p['lossMarketValue'],p['declineMarketValue'])==(10,1,9)
    assert p['valuationDate']==q['valuationDate']=='2024-05-06' and q['valuationAnchor']=='2024-04-30'
    assert q['lossMembers']==[data.symbols[0]] and q['declineMembers']==[data.symbols[1]]
    assert (p['sampleCount'],p['candidateCount'],p['coverage'])==(2,2,100)
    assert valid_enterprise_analysis(v) and repr(data)==before


@pytest.mark.parametrize('current,old,want',[
    ((-3,-1),(-2,2),(100,100,0)),
    ((0,1),(0,0),(0,0,0)),
    ((-1,0),(0,0),(10,10,0)),
])
def test_overlapping_sets_negative_and_zero_prior_are_not_growth_bases(current,old,want):
    v=result(loss_inputs(current,old));p=v['cards'][-1]['points'][-1]
    assert (p['primary'],p['secondary'],p['result'])==want
    assert valid_enterprise_analysis(v)


def test_only_profit_is_needed_not_revenue_cash_or_balance():
    data=loss_inputs()
    rows=tuple(replace(r,values={'n_income_attr_p':r.values['n_income_attr_p']}) for r in data.financials if r.table=='income')
    v=result(replace(data,financials=rows)); assert v['cards'][-1]['points'][-1]['primary']==10
    assert not v['cards'][0]['points'] and valid_enterprise_analysis(v)


def test_q2_subtracts_both_years_q1_and_uses_separate_valuation_date():
    data=loss_inputs(); rows=[]
    for year,profits in ((2023,(-4,6)),(2024,(-4,4))):
        rows.extend(FinancialRecord('income',s,date(year,6,30),date(year,8,31),None,'1','1','1',
            {'n_income_attr_p':profits[i]},f'{year}q2:{s}') for i,s in enumerate(data.symbols))
    days=(*data.trading_days,enterprise.TradingDayInput(date(2024,9,1),False,'sun'),enterprise.TradingDayInput(date(2024,9,2),True,'mon'))
    caps=tuple(replace(r,trade_date=date(2024,9,2)) for r in data.market_values)
    p=result(replace(data,financials=(*data.financials,*rows),trading_days=days,market_values=caps),date(2024,9,30))['cards'][-1]['points'][-1]
    assert (p['primary'],p['secondary'],p['result'],p['valuationDate'])==(10,100,-90,'2024-09-02')


@pytest.mark.parametrize('damage,reason',[
    ('missing-cap','missing-market-value'),('wrong-date','missing-market-value'),('zero','invalid-market-value'),
    ('negative','invalid-market-value'),('nan','invalid-market-value'),('bool','invalid-market-value'),
    ('missing-profit','missing-report'),('late','missing-report'),('conflict','revision-conflict'),
    ('industry','industry-history-missing-or-changed'),('listing','pre-listing-input'),
])
def test_missing_bad_or_incompatible_input_remains_candidate(damage,reason):
    data=loss_inputs();symbol=data.symbols[0]
    if damage=='missing-cap': data=replace(data,market_values=data.market_values[1:])
    elif damage=='wrong-date': data=replace(data,market_values=tuple(replace(r,trade_date=r.trade_date+timedelta(days=1)) if r.symbol==symbol else r for r in data.market_values))
    elif damage in ('zero','negative','nan','bool'):
        bad={'zero':0,'negative':-1,'nan':'NaN','bool':True}[damage]
        data=replace(data,market_values=tuple(replace(r,total_mv=bad) if r.symbol==symbol else r for r in data.market_values))
    elif damage=='industry':
        data=replace(data,industries=tuple(replace(r,effective_from=date(2024,1,1)) if r.symbol==symbol else r for r in data.industries))
    elif damage=='listing':
        data=replace(data,securities=tuple(replace(r,list_date=date(2024,1,1)) if r.symbol==symbol else r for r in data.securities))
    else:
        target=next(r for r in data.financials if r.symbol==symbol and r.table=='income' and r.end_date.year==2024)
        rows=list(data.financials)
        if damage=='missing-profit': rows.remove(target)
        elif damage=='late': rows[rows.index(target)]=replace(target,f_ann_date=date(2024,5,7))
        else: rows.append(replace(target,values={**target.values,'n_income_attr_p':999},source_key='conflict'))
        data=replace(data,financials=tuple(rows))
    v=result(data);c=v['cards'][-1];q=c['quality'][-1]
    assert not c['points'] and (q['sampleCount'],q['candidateCount'],q['coverage'])==(1,2,50)
    assert q['excluded'][symbol]==reason and valid_enterprise_analysis(v)


def test_later_revision_is_not_injected_into_old_market_value():
    data=loss_inputs();target=next(r for r in data.financials if r.table=='income' and r.end_date.year==2024 and r.symbol==data.symbols[0])
    revised=replace(target,values={**target.values,'n_income_attr_p':100},f_ann_date=date(2024,5,7),source_key='later-revision')
    assert result(replace(data,financials=(*data.financials,revised)))['cards'][-1]==result(data)['cards'][-1]


@pytest.mark.parametrize('damage,status',[
    ('gap','valuation-calendar-missing'),('not-due','valuation-window-not-due'),('same-day','valuation-day-not-complete'),
])
def test_calendar_gap_or_not_observed_never_postpones_to_a_convenient_market_day(damage,status):
    data=loss_inputs();as_of=AS_OF
    if damage=='gap': data=replace(data,trading_days=data.trading_days[1:])
    elif damage=='not-due': as_of=date(2024,4,20)
    else: as_of=date(2024,5,6)
    v=result(data,as_of);c=v['cards'][-1];q=c['quality'][-1]
    assert not c['points'] and q['valuationDate'] is None and q['valuationStatus']==status
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('size',[61,5000])
def test_dynamic_scope_not_capped_at_development_pool(size):
    data=loss_inputs();symbols=tuple(f'{i:06}.SZ' for i in range(size));template=data.symbols[0]
    data=replace(data,symbols=symbols,
        securities=tuple(replace(data.securities[0],symbol=s) for s in symbols),
        industries=tuple(replace(data.industries[0],symbol=s) for s in symbols),
        financials=tuple(replace(r,symbol=s,source_key=f'{r.source_key}:{s}') for s in symbols for r in data.financials if r.symbol==template),
        market_values=tuple(replace(data.market_values[0],symbol=s,source_key=f'market:{s}') for s in symbols))
    p=result(data)['cards'][-1]['points'][-1]
    assert (p['sampleCount'],p['candidateCount'],p['coverage'],p['primary'],p['secondary'])==(size,size,100,100,0)


@pytest.mark.parametrize('weights',[('1e-10000',90000),('1e10000',90000)])
def test_unrepresentable_market_totals_do_not_emit_fake_zero(weights):
    v=result(loss_inputs(weights=weights));c=v['cards'][-1]
    assert not c['points'] and c['quality'][-1]['status']=='non-finite-aggregate'
    assert valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['date','total','loss','decline','subset','unit','ratio','count'])
def test_public_contract_rejects_inconsistent_loss_evidence(damage):
    v=result();c=v['cards'][-1];p=c['points'][-1];q=c['quality'][-1]
    if damage=='date': p['valuationDate']='2024-05-07'
    elif damage=='total': p['totalMarketValue']=0
    elif damage=='loss': p['lossMarketValue']=11
    elif damage=='decline': p['declineMarketValue']=-1
    elif damage=='subset': q['lossMembers']=['outside']
    elif damage=='unit': c['marketValueUnit']='万元'
    elif damage=='ratio': p['primary']=11;p['result']=-79
    else: q['sampleCount']=1
    assert not valid_enterprise_analysis(v)


def test_q4_is_annual_minus_q3_on_next_april_window_not_current_april():
    data=loss_inputs();rows=[]
    for year,q3,q4 in ((2023,(-1,3),(-3,6)),(2024,(-1,3),(-4,4))):
        for month,day,profits in ((9,30,q3),(12,31,q4)):
            for i,s in enumerate(data.symbols):
                rows.append(FinancialRecord('income',s,date(year,month,day),date(year+1,4,30),None,'1','1','1',
                    {'n_income_attr_p':profits[i]},f'{year}:{month}:{s}'))
    days=tuple(enterprise.TradingDayInput(date(2025,5,d),d==6,f'2025:{d}') for d in range(1,7))
    caps=tuple(replace(r,trade_date=date(2025,5,6)) for r in data.market_values)
    v=result(replace(data,financials=tuple(rows),trading_days=days,market_values=caps),date(2025,3,31))
    assert not v['cards'][-1]['points']
    v=result(replace(data,financials=tuple(rows),trading_days=days,market_values=caps),date(2025,5,31),EnterprisePolicy(min_samples=1,max_quarters=2))
    p=v['cards'][-1]['points'][0]
    assert (p['period'],p['primary'],p['secondary'],p['result'],p['valuationDate'])==('2024-12-31',10,100,-90,'2025-05-06')
    assert valid_enterprise_analysis(v)


def test_unrelated_market_dates_and_calendar_suffix_do_not_change_input_hash():
    data=loss_inputs();v=result(data)
    extra=replace(data,market_values=(*data.market_values,replace(data.market_values[0],trade_date=date(2024,5,7),total_mv=999999)),
        trading_days=(*data.trading_days,enterprise.TradingDayInput(date(2024,5,7),True,'unused')))
    assert result(extra)==v


def test_conflicting_calendar_does_not_pick_arbitrary_open_status():
    data=loss_inputs();extra=enterprise.TradingDayInput(date(2024,5,1),True,'conflicting')
    v=result(replace(data,trading_days=(*data.trading_days,extra)))
    assert not v['cards'][-1]['points'] and v['cards'][-1]['quality'][-1]['valuationStatus']=='valuation-calendar-conflict'
    assert valid_enterprise_analysis(v)


def test_financial_industry_is_not_a_missing_or_zero_profit_sample():
    data=loss_inputs();s=data.symbols[0]
    v=result(replace(data,industries=tuple(replace(r,root_code='801790.SI') if r.symbol==s else r for r in data.industries)))
    p=v['cards'][-1]['points'][-1];q=v['cards'][-1]['quality'][-1]
    assert (p['primary'],p['secondary'],p['candidateCount'],p['sampleCount'])==(0,100,1,1)
    assert q['nonApplicable']=={s:'financial-industry-review'} and valid_enterprise_analysis(v)


@pytest.mark.parametrize('damage',['switch','requires-distribution','outside','conflicting-cap'])
def test_loss_input_identity_or_switch_cannot_be_silently_coerced(damage):
    data=loss_inputs()
    if damage=='switch': data=replace(data,loss_enabled=1)
    elif damage=='requires-distribution': data=replace(data,distribution_enabled=False)
    elif damage=='outside': data=replace(data,market_values=(*data.market_values,replace(data.market_values[0],symbol='outside')))
    else:
        data=replace(data,market_values=(*data.market_values,replace(data.market_values[0],total_mv=999,source_key='conflict')))
        v=result(data);assert not v['cards'][-1]['points'] and v['cards'][-1]['quality'][-1]['excluded'][data.symbols[0]]=='market-value-conflict'
        return
    with pytest.raises(ValueError): result(data)


@pytest.mark.parametrize('damage',['full-loss','full-decline','equal-sets','disjoint-over','union-under','underflow'])
def test_loss_public_contract_rejects_impossible_set_weights_and_positive_false_zero(damage):
    v=result();c=v['cards'][-1];p=c['points'][-1];q=c['quality'][-1]
    if damage=='full-loss': q['lossMembers']=q['included'][:]
    elif damage=='full-decline': q['declineMembers']=q['included'][:]
    elif damage=='equal-sets': q['declineMembers']=q['lossMembers'][:]
    elif damage=='disjoint-over': p['declineMarketValue']=10;p['secondary']=100;p['result']=-90
    elif damage=='union-under': p['declineMarketValue']=8;p['secondary']=80;p['result']=-70
    else:
        p.update(totalMarketValue=1e300,lossMarketValue=1e-300,declineMarketValue=1e300,primary=0,secondary=100,result=-100)
    assert not valid_enterprise_analysis(v)
