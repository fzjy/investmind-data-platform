"""池内行业不能冒充全行业；历史/重叠/null与既有门槛必须保留。"""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import importlib

import pytest

from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_rea import controlled, metric

AS_OF=date(2024,8,1)
POLICY=EnterprisePolicy(min_samples=5)


def inputs(count=61):
    module=importlib.import_module('app.macro.enterprise')
    assert hasattr(module,'IndustryLabelInput'), 'industry classification labels are not available'
    data=controlled(count)
    return replace(data,industries=tuple(replace(r,root_code='801120.SI' if i%2 else '801080.SI')
        for i,r in enumerate(data.industries)),industry_labels=(
            module.IndustryLabelInput('801080.SI','电子','classification:1'),
            module.IndustryLabelInput('801120.SI','食品饮料','classification:2')))


def build(data=None,policy=POLICY):
    return public_enterprise_analysis(data or inputs(),AS_OF,policy,rea_enabled=True,industry_enabled=True)


def test_industry_partition_dynamic_members_and_existing_results_unchanged():
    data=inputs();plain=public_enterprise_analysis(data,AS_OF,POLICY,rea_enabled=True)
    result=build(data);comparison=result['industryComparison']
    assert {k:v for k,v in result.items() if k!='industryComparison'}==plain
    assert valid_enterprise_analysis(result)
    assert comparison['version']=='enterprise-industry-v1'
    assert comparison['weightingMethod']=='amount-aggregate-not-market-cap-weighted'
    assert comparison['unassigned']=={}
    assert [(g['code'],g['name'],g['analysis']['poolCount']) for g in comparison['groups']]==[
        ('801080.SI','电子',31),('801120.SI','食品饮料',30)]
    for g in comparison['groups']:
        assert 'industryComparison' not in g['analysis']
        p=metric(g['analysis']['rea'],'capacity')['points'][-1]
        assert p['sampleCount']==g['analysis']['poolCount'] and p['value']==0


def test_unknown_overlap_and_changed_industry_history_are_not_reassigned():
    data=inputs(12)
    rows=list(data.industries)
    rows[0]=replace(rows[0],root_code=None)
    rows.append(replace(rows[1],root_code='801080.SI',source_key='overlap'))
    rows[2]=replace(rows[2],effective_from=date(2024,1,1))
    result=build(replace(data,industries=tuple(rows)))['industryComparison']
    assert set(result['unassigned'])=={data.symbols[0],data.symbols[1]}
    group=next(g for g in result['groups'] if g['code']=='801080.SI')
    q=metric(group['analysis']['rea'],'capacity')['quality'][-1]
    assert q['candidateCount']==5 and q['sampleCount']==4 and q['status']=='insufficient-samples'
    assert q['excluded'][data.symbols[2]]=='industry-history-missing-or-changed'


def test_current_groups_keep_missing_reports_in_denominator_and_do_not_lower_policy():
    data=inputs(10)
    data=replace(data,financials=tuple(r for r in data.financials if not(r.symbol==data.symbols[0] and r.table=='income' and r.end_date==date(2024,6,30))))
    comparison=build(data)['industryComparison']
    group=comparison['groups'][0]
    q=metric(group['analysis']['rea'],'capacity')['quality'][-1]
    assert q['candidateCount']==5 and q['sampleCount']==4
    assert q['status']=='insufficient-samples' and group['analysis']['policy']['min_samples']==5


@pytest.mark.parametrize('mode',['overlap','previous-root'])
def test_current_group_cannot_hide_conflicting_or_different_historical_roots(mode):
    data=inputs(10);member=data.symbols[0];rows=list(data.industries)
    if mode=='previous-root': rows[0]=replace(rows[0],effective_from=date(2024,7,1))
    rows.append(replace(rows[0],root_code='801120.SI',effective_from=date(2020,1,1),
        effective_to=date(2023,6,30) if mode=='overlap' else date(2024,6,30),source_key='actual-old-root'))
    group=build(replace(data,industries=tuple(rows)))['industryComparison']['groups'][0]
    for identifier in ('capacity','employee'):
        q=metric(group['analysis']['rea'],identifier)['quality'][-1]
        assert q['sampleCount']==4 and member in q['excluded'],mode


def test_selected_root_restriction_also_protects_legacy_cards_ledger_and_valued_loss():
    from app.macro.enterprise import MarketValueInput,TradingDayInput
    data=inputs(10);member=data.symbols[0];rows=list(data.industries)
    rows[0]=replace(rows[0],effective_from=date(2024,7,1))
    rows.append(replace(rows[0],root_code='801120.SI',effective_from=date(2020,1,1),effective_to=date(2024,6,30)))
    data=replace(data,industries=tuple(rows),financials=tuple(replace(r,values={**r.values,**(
        {'n_income_attr_p':r.values['n_income']} if r.table=='income' else {'inventories':10,'accounts_receiv':10} if r.table=='balance' else {})}) for r in data.financials),
        ledger_enabled=True,orders_enabled=True,loss_enabled=True,cashflow_enabled=True,distribution_enabled=True,
        trading_days=(TradingDayInput(date(2024,9,1),False,'cal:1'),TradingDayInput(date(2024,9,2),True,'cal:2')),
        market_values=tuple(MarketValueInput(s,date(2024,9,2),100,'mv:'+s) for s in data.symbols))
    result=public_enterprise_analysis(data,date(2024,12,1),POLICY,rea_enabled=True,industry_enabled=True)
    group=result['industryComparison']['groups'][0]['analysis']
    for metrics in (group['cards'],group['ledger']['metrics'],group['rea']['series']):
        for m in metrics:
            q=next(q for q in m['quality'] if q['period']=='2024-06-30')
            assert member not in q['included'],m['id']
            assert q['excluded'][member]=='outside-selected-current-industry',m['id']


def test_code_fallback_is_explicit_and_conflicting_labels_are_rejected():
    data=inputs(10)
    result=build(replace(data,industry_labels=()))['industryComparison']
    assert result['groups'][0]['name']=='801080.SI' and result['groups'][0]['labelSource']=='code-fallback'
    with pytest.raises(ValueError,match='INDUSTRY_LABEL_INVALID'):
        build(replace(data,industry_labels=data.industry_labels+data.industry_labels[:1]))


def test_entire_industry_without_financials_keeps_parent_calendar_and_missing_denominator():
    data=inputs(10);missing=set(data.symbols[::2])
    result=build(replace(data,financials=tuple(r for r in data.financials if r.symbol not in missing)))
    group=result['industryComparison']['groups'][0]['analysis']
    for series in group['rea']['series']:
        q=next((q for q in series['quality'] if q['period']=='2024-06-30'),None)
        assert q is not None,'missing industry must keep parent calendar'
        assert q['sampleCount']==0 and q['candidateCount']==5
        assert sorted(q['excluded'])==sorted(missing)
    for card in group['cards']:
        q=next(q for q in card['quality'] if q['period']=='2024-06-30')
        assert q['sampleCount']==0 and q['candidateCount']==5


def test_industry_cannot_downgrade_parent_algorithm_and_drop_cards_or_ledger():
    result=build();g=result['industryComparison']['groups'][0]['analysis']
    # 当前controlled是旧版本；切换一个合法但不同的子版本同样必须拒绝。
    g['algorithmVersion']='enterprise-quarter-v2'
    extra=deepcopy(g['cards'][0]);extra.update(id='white-strip',title='宏观白条指数')
    g['cards'].append(extra)
    assert not valid_enterprise_analysis(result)


@pytest.mark.parametrize('damage',['overlap','missing','policy','date','recursive','name','coverage'])
def test_comparison_contract_rejects_inconsistent_nested_inputs(damage):
    result=deepcopy(build());c=result['industryComparison'];g=c['groups'][0]
    if damage=='overlap': c['groups'][1]['analysis']['symbols'][0]=g['analysis']['symbols'][0]
    elif damage=='missing': c['groups'].pop()
    elif damage=='policy': g['analysis']['policy']['min_samples']=1
    elif damage=='date': c['snapshotDate']='2024-08-02'
    elif damage=='recursive': g['analysis']['industryComparison']=deepcopy(c)
    elif damage=='name': g['name']='伪造名称'
    elif damage=='coverage': g['analysis']['rea']['series'][3]['quality'][-1]['fieldCoverage']['balance.fix_assets']+=1
    assert not valid_enterprise_analysis(result),damage


def test_per_field_coverage_includes_all_required_reports_and_negative_quarter_checks():
    data=inputs(10)
    data=replace(data,financials=tuple(replace(r,values={**r.values,**(
        {'fix_assets':None} if r.table=='balance' else {'c_paid_to_for_empl':1})})
        if r.symbol==data.symbols[0] and r.end_date==date(2024,6,30) and r.table in ('balance','cashflow') else r for r in data.financials))
    result=build(data)
    capacity=metric(result['rea'],'capacity')['quality'][-1]
    assert capacity['fieldCoverage']['balance.fix_assets']==9
    assert capacity['fieldCoverage']['income.revenue']==10
    employee=metric(result['rea'],'employee')['quality'][-1]
    assert employee['fieldCoverage']['cashflow.c_paid_to_for_empl']==9
    assert employee['candidateCount']==10


@pytest.mark.parametrize('damage',['future','source','null'])
def test_field_coverage_does_not_count_unusable_supplements(damage):
    from backend.tests.macro.test_enterprise_rea_supplement import supplemented
    _,data=supplemented(10)
    changes={'available_date':date(2024,8,2)} if damage=='future' else {'source_hash':'0'*64} if damage=='source' else {'status':'returned-null','value':None}
    data=replace(data,rea_fields=tuple(replace(e,**changes) if e.field=='fix_assets' else e for e in data.rea_fields))
    q=metric(build(data)['rea'],'capacity')['quality'][-1]
    assert q['fieldCoverage']['balance.fix_assets']==0 and q['fieldCoverage']['income.revenue']==10
