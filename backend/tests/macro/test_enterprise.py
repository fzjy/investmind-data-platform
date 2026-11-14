from dataclasses import replace
from datetime import date, timedelta
import importlib
import importlib.util

import pytest


def module():
    if importlib.util.find_spec('app.macro.enterprise') is None:
        pytest.fail('enterprise foundation is not implemented')
    return importlib.import_module('app.macro.enterprise')


def inputs(symbols=('000001.SZ','000002.SZ')):
    m=module()
    records=[]
    for symbol,previous,current,old_profit,new_profit,old_stock,new_stock in (
        ('000001.SZ',100,110,10,5,100,120),
        ('000002.SZ',900,990,-5,0,900,990),
    ):
        if symbol not in symbols: continue
        for year,revenue,profit,stock in ((2023,previous,old_profit,old_stock),(2024,current,new_profit,new_stock)):
            period=date(year,3,31)
            for table,values in (('income',{'revenue':revenue,'n_income_attr_p':profit}),('balance',{'inventories':stock})):
                records.append(m.FinancialRecord(table,symbol,period,period+timedelta(days=30),None,'1','1','1',values,f'{table}:{symbol}:{year}'))
    return m.EnterpriseInputs('test-pool',symbols,
        tuple(m.SecurityInput(s,date(2020,1,1)) for s in symbols),tuple(records),
        tuple(m.IndustryInput(s,'801120.SI',date(2020,1,1),None) for s in symbols))


def build(data=None,as_of=date(2024,5,31),**policy):
    m=module()
    return m.build_enterprise_analysis(data or inputs(),as_of,m.EnterprisePolicy(min_samples=1,**policy))


def card(result,identifier='scissors'):
    return next(c for c in result['cards'] if c['id']==identifier)


def test_group_totals_not_mean_yoy_keep_loss_zero_and_input_unchanged():
    data=inputs(); before=repr(data)
    result=card(build(data))
    assert result['points'][-1] == {'period':'2024-03-31','quarter':'2024Q1','primary':10.0,'secondary':0.0,'result':10.0,
        'sampleCount':2,'candidateCount':2,'coverage':100.0,'sampleSetChanged':False}
    quality=result['quality'][-1]
    assert quality['included']==['000001.SZ','000002.SZ']
    assert quality['negativeIndividualBases']==['000002.SZ']
    assert all(ref['sourceKey'] for ref in quality['sourceRefs'])
    assert repr(data)==before


def test_inventory_is_period_end_stock_not_quarter_difference():
    point=card(build(),'inventory')['points'][-1]
    assert (point['primary'],point['secondary'],point['result'])==(10.0,11.0,-1.0)


@pytest.mark.parametrize('bad',[None,True,'NaN','Infinity'])
def test_invalid_value_is_missing_not_zero_and_missing_stock_stays_denominator(bad):
    data=inputs(); rows=list(data.financials)
    rows=[replace(r,values={**r.values,'revenue':bad}) if r.symbol=='000001.SZ' and r.table=='income' and r.end_date.year==2024 else r for r in rows]
    c=card(build(replace(data,financials=tuple(rows))))
    assert c['points']==[]
    q=c['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'],q['status'])==(1,2,'insufficient-coverage')


@pytest.mark.parametrize('old_profit',[-10,0])
def test_non_positive_aggregate_base_rejects_whole_point_not_individual(old_profit):
    data=inputs()
    rows=tuple(replace(r,values={**r.values,'n_income_attr_p':old_profit}) if r.table=='income' and r.end_date.year==2023 else r for r in data.financials)
    c=card(build(replace(data,financials=rows)))
    assert c['points']==[]
    assert c['quality'][-1]['status']=='non-positive-aggregate-base'
    assert c['quality'][-1]['sampleCount']==2


def test_missing_preceding_ytd_does_not_treat_h1_as_q2():
    m=module(); data=inputs(('000001.SZ',)); rows=list(data.financials)
    for year,rev,profit in ((2023,250,25),(2024,300,30)):
        p=date(year,6,30)
        rows.append(m.FinancialRecord('income','000001.SZ',p,p+timedelta(days=30),None,'1','1','1',{'revenue':rev,'n_income_attr_p':profit},f'q2:{year}'))
    c=card(build(replace(data,financials=tuple(rows)),as_of=date(2024,8,1)))
    assert c['points'][-1]['quarter']=='2024Q2'
    # current Q2=300-110=190, previous Q2=250-100=150; profit=25/15.
    assert c['points'][-1]['primary']==pytest.approx(26.666666666666668)
    rows=[r for r in rows if not (r.table=='income' and r.end_date==date(2024,3,31))]
    c=card(build(replace(data,financials=tuple(rows)),as_of=date(2024,8,1)))
    assert c['quality'][-1]['status']=='insufficient-samples' and c['points']==[]


def test_future_actual_or_declared_announcement_cannot_override_visible_revision():
    data=inputs(); original=next(r for r in data.financials if r.table=='income' and r.end_date.year==2024)
    future=replace(original,ann_date=date(2027,1,1),f_ann_date=date(2024,4,30),values={'revenue':99999,'n_income_attr_p':99999})
    result=card(build(replace(data,financials=(*data.financials,future))))
    assert result['points'][-1]['primary']==10.0


def test_equal_rank_conflict_is_not_silently_chosen_or_reverted():
    data=inputs(); original=next(r for r in data.financials if r.table=='income' and r.end_date.year==2024)
    conflict=replace(original,values={**original.values,'revenue':99999},source_key='conflict')
    c=card(build(replace(data,financials=(*data.financials,conflict))))
    assert c['points']==[]
    assert c['quality'][-1]['excluded']['000001.SZ']=='revision-conflict'


def test_mother_company_cannot_fill_missing_consolidated_period():
    data=inputs()
    rows=tuple(replace(r,report_type='6') if r.symbol=='000001.SZ' and r.end_date.year==2024 else r for r in data.financials)
    assert card(build(replace(data,financials=rows)))['points']==[]


def test_financial_industry_mismatch_is_non_applicable_not_industrial():
    data=inputs()
    industry=replace(data.industries[0],root_code='801790.SI')
    c=card(build(replace(data,industries=(industry,data.industries[1]))))
    q=c['quality'][-1]
    assert q['candidateCount']==1 and q['sampleCount']==1
    assert q['nonApplicable']['000001.SZ']=='financial-industry-review'


@pytest.mark.parametrize('kind',['missing','change','overlap'])
def test_unknown_or_changing_industry_cannot_sneak_into_yoy_sample(kind):
    m=module();data=inputs()
    industries=[i for i in data.industries if i.symbol!='000001.SZ']
    if kind=='change':
        industries += [m.IndustryInput('000001.SZ','801120.SI',date(2020,1,1),date(2023,12,31)),m.IndustryInput('000001.SZ','801080.SI',date(2024,1,1),None)]
    if kind=='overlap': industries += [data.industries[0],replace(data.industries[0],root_code='801080.SI')]
    c=card(build(replace(data,industries=tuple(industries))))
    assert c['points']==[] and c['quality'][-1]['candidateCount']==2


def test_unknown_listing_is_not_assumed_listed_and_st_requires_no_exclusion():
    data=inputs(); securities=(replace(data.securities[0],list_date=None),data.securities[1])
    q=card(build(replace(data,securities=securities)))['quality'][-1]
    assert q['excluded']['000001.SZ']=='unknown-listing-date'
    assert q['candidateCount']==2


def test_min_samples_and_window_are_applied_without_fabricating_quarters():
    m=module()
    c=card(m.build_enterprise_analysis(inputs(),date(2026,10,18),m.EnterprisePolicy(min_samples=3,max_quarters=2)))
    assert c['points']==[] and len(c['quality'])==2
    assert c['quality'][-1]['status']=='insufficient-samples'


def test_out_of_pool_record_and_duplicate_pool_rejected():
    data=inputs()
    with pytest.raises(ValueError): build(replace(data,symbols=(*data.symbols,data.symbols[0])))
    with pytest.raises(ValueError): build(replace(data,financials=(*data.financials,replace(data.financials[0],symbol='999999.SH'))))


def test_order_independent_output_and_source_identity_changes_are_auditable():
    data=inputs(); original=build(data)
    assert build(replace(data,financials=tuple(reversed(data.financials))))==original
    rows=(replace(data.financials[0],source_key='different-core-row'),*data.financials[1:])
    assert build(replace(data,financials=rows))['inputHash']!=original['inputHash']


def test_latest_closed_quarter_keeps_unreported_quality_not_fake_point():
    c=card(build(as_of=date(2026,10,18)))
    assert c['quality'][-1]['period']=='2026-09-30'
    assert c['quality'][-1]['status']=='insufficient-samples'
    assert c['points'][-1]['period']=='2024-03-31'


def test_company_type_conflict_stays_in_denominator_not_hidden_as_financial():
    data=inputs()
    rows=tuple(replace(r,comp_type='2') if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024 else r for r in data.financials)
    q=card(build(replace(data,financials=rows)))['quality'][-1]
    assert q['candidateCount']==2
    assert q['excluded']['000001.SZ']=='company-type-conflict'


@pytest.mark.parametrize('conflicting_table',['cashflow','income'])
def test_current_revision_conflict_is_checked_before_financial_exclusion(conflicting_table):
    m=module();data=inputs()
    a=m.FinancialRecord(conflicting_table,'000001.SZ',date(2024,3,31),date(2024,4,30),None,'1','1','1',{'revenue':110,'n_income_attr_p':5},'conflict-a')
    b=replace(a,comp_type='2',source_key='conflict-b')
    rows=[r for r in data.financials if not(r.symbol==a.symbol and r.table==conflicting_table and r.end_date==a.end_date)]
    if conflicting_table=='income':
        rows=[replace(r,comp_type='2') if r.symbol==a.symbol and r.table=='balance' and r.end_date==a.end_date else r for r in rows]
    q=card(build(replace(data,financials=(*rows,a,b))))['quality'][-1]
    assert q['candidateCount']==2 and q['sampleCount']==1
    assert q['excluded']['000001.SZ']=='revision-conflict'
    assert {'conflict-a','conflict-b'} <= {r['sourceKey'] for r in q['sourceRefs']}


def test_financial_and_type_conflict_decisions_keep_all_current_core_evidence():
    m=module();data=inputs()
    cash=m.FinancialRecord('cashflow','000001.SZ',date(2024,3,31),date(2024,4,30),None,'1','2','1',{},'cash-decision')
    q=card(build(replace(data,financials=(*data.financials,cash))))['quality'][-1]
    refs=[r for r in q['sourceRefs'] if r['symbol']=='000001.SZ']
    assert {r['table'] for r in refs}=={'income','balance','cashflow'}
    assert all('compType' in r and 'reportType' in r and r['visibleAt'] for r in refs)
    assert q['excluded']['000001.SZ']=='company-type-conflict'


def test_prior_revision_conflict_keeps_both_sources_in_quality():
    data=inputs()
    prior=next(r for r in data.financials if r.table=='income' and r.symbol=='000001.SZ' and r.end_date.year==2023)
    conflict=replace(prior,values={**prior.values,'revenue':200},source_key='prior-conflict')
    q=card(build(replace(data,financials=(*data.financials,conflict))))['quality'][-1]
    assert q['excluded']['000001.SZ']=='revision-conflict'
    assert {prior.source_key,'prior-conflict'} <= {r['sourceKey'] for r in q['sourceRefs']}
