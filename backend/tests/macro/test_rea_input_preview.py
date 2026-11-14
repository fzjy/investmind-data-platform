from dataclasses import replace
from datetime import date
import importlib
import importlib.util

import pytest

from app.macro.enterprise import EnterprisePolicy
from app.connectors.tushare.strategies import FIELD_REGISTRY, LEGACY_CASHFLOW_FIELDS, Dataset
from backend.tests.macro.test_enterprise_rea import controlled
from app.macro.receivables_preview import preview_receivables


def module():
    assert importlib.util.find_spec('app.macro.rea_input_preview'), 'REA input preview not implemented'
    return importlib.import_module('app.macro.rea_input_preview')


def data(count=2):
    original=controlled(count)
    removed={'fix_assets','acct_payable','c_paid_to_for_empl','depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp'}
    metadata={'ts_code','ann_date','f_ann_date','end_date','report_type','comp_type','update_flag'}
    def values(row):
        dataset=Dataset.BALANCE_SHEET if row.table=='balance' else Dataset.CASHFLOW if row.table=='cashflow' else None
        fields=LEGACY_CASHFLOW_FIELDS if row.table=="cashflow" else FIELD_REGISTRY.get(dataset, ())
        witness={field:None for field in fields if field not in metadata}
        return {k:v for k,v in {**witness,**row.values}.items() if k not in removed}
    return replace(original,financials=tuple(replace(r,values=values(r)) for r in original.financials))


def preview(inputs=None,as_of=date(2024,8,1),quarters=1,limit=100,policy=None):
    return module().preview_rea_inputs(inputs or data(),as_of,policy or EnterprisePolicy(min_samples=1),quarters,limit)


def test_q2_sources_include_current_and_prior_year_q1_cumulative_but_balance_never_differences():
    result=preview()
    assert result['outputPeriods']==['2024-06-30']
    assert result['sourcePeriods']=={'balance':['2023-06-30','2024-06-30'],
        'cashflow':['2023-03-31','2023-06-30','2024-03-31','2024-06-30']}
    assert result['logicalRequestCount']==12
    identities={(r['table'],r['params']['ts_code'],r['params']['period']) for r in result['requests']}
    assert len(identities)==12
    assert result['physicalHttpExecuted']==0 and result['readOnly']
    assert not result['physicalBudgetEnforced'] and not result['persistenceImplemented'] and not result['permissionsVerified']
    assert set(result['nodeIds'])=={'capacity','renewal','employee','payable','debt'}


def test_q1_does_not_subtract_last_year_q4_and_no_successful_card_required():
    result=preview(as_of=date(2024,5,1))
    assert result['outputPeriods']==['2024-03-31']
    assert result['sourcePeriods']=={'balance':['2023-03-31','2024-03-31'],'cashflow':['2023-03-31','2024-03-31']}
    empty=preview(replace(data(),financials=()))
    assert empty['outputPeriods']==['2024-06-30']
    assert empty['logicalRequestCount']==0
    assert empty['decisionCounts']=={'core-version-missing':12}


def test_q4_and_multi_quarter_window_avoid_duplicate_requests_and_obey_policy():
    result=preview(as_of=date(2025,2,1),quarters=8,policy=EnterprisePolicy(min_samples=1,max_quarters=2))
    assert result['outputPeriods']==['2024-09-30','2024-12-31']
    assert result['sourcePeriods']['cashflow']==['2023-06-30','2023-09-30','2023-12-31','2024-06-30','2024-09-30','2024-12-31']
    assert len({r['requestKey'] for r in result['requests']})==len(result['requests'])


@pytest.mark.parametrize('value,status',[(None,'existing-null-field'),(0,'existing-finite-field'),
    (-1,'invalid-existing-field'),(True,'invalid-existing-field'),('NaN','invalid-existing-field')])
def test_existing_values_do_not_become_refetches_or_zero(value,status):
    original=data()
    changed=replace(original,financials=tuple(replace(r,values={**r.values,'fix_assets':value,'acct_payable':0})
        if r.table=='balance' else r for r in original.financials))
    result=preview(changed)
    assert all(r['table']=='cashflow' for r in result['requests'])
    balances=[d for d in result['decisions'] if d['table']=='balance']
    assert all(d['fieldStates']['fix_assets']==status for d in balances)
    assert all(d['fieldStates']['acct_payable']=='existing-finite-field' for d in balances)


def test_closed_requests_do_not_change_registry_or_old_supplement_identity():
    from app.macro.balance_supplement import ORDERS
    original=data()
    registry=dict(FIELD_REGISTRY)
    old=preview_receivables(original,date(2024,8,1),EnterprisePolicy(min_samples=1),8,100)
    orders=preview_receivables(original,date(2024,8,1),EnterprisePolicy(min_samples=1),8,100,target=ORDERS)
    result=preview(original)
    for request in result['requests']:
        dataset=Dataset.BALANCE_SHEET if request['table']=='balance' else Dataset.CASHFLOW
        witnesses = LEGACY_CASHFLOW_FIELDS if dataset == Dataset.CASHFLOW else FIELD_REGISTRY[dataset]
        assert set(witnesses)<=set(request['fields'])
        assert request['params']['report_type']=='1'
        assert request['apiName'] in ('balancesheet','cashflow')
        assert len(request['requestKey'])==64
    assert FIELD_REGISTRY==registry
    assert preview_receivables(original,date(2024,8,1),EnterprisePolicy(min_samples=1),8,100)==old
    assert preview_receivables(original,date(2024,8,1),EnterprisePolicy(min_samples=1),8,100,target=ORDERS)==orders


def test_stable_request_identity_and_budget_only_changes_plan():
    original=data()
    a=preview(original)
    reordered=replace(original,symbols=tuple(reversed(original.symbols)),financials=tuple(reversed(original.financials)),industries=tuple(reversed(original.industries)))
    assert preview(reordered,as_of=date(2024,8,2))['planFingerprint']==a['planFingerprint']
    b=preview(original,limit=1)
    assert b['requests']==a['requests'] and b['planFingerprint']!=a['planFingerprint']
    assert len(b['nextBatchCandidates'])==1
    changed=replace(original,industries=tuple(replace(i,root_code='801130.SI') for i in original.industries))
    assert preview(changed)['planFingerprint']!=a['planFingerprint']
    changed=replace(original,financials=tuple(replace(r,values={**r.values,'money_cap':123}) if r.table=='balance' else r for r in original.financials))
    assert {r['requestKey'] for r in preview(changed)['requests']}!={r['requestKey'] for r in a['requests']}


@pytest.mark.parametrize('case,reason',[('financial','financial-report'),('unknown','unknown-or-overlapping-industry'),
    ('conflict','revision-conflict'),('late','core-version-missing'),('before-listing','before-listing'),('witness','source-witness-missing')])
def test_ineligible_core_is_a_decision_not_a_supplier_request(case,reason):
    original=data()
    if case=='financial': original=replace(original,financials=tuple(replace(r,comp_type='2') for r in original.financials))
    elif case=='unknown': original=replace(original,industries=())
    elif case=='conflict': original=replace(original,financials=(*original.financials,*(replace(r,values={**r.values,'inventories':999},source_key=r.source_key+':other') for r in original.financials)))
    elif case=='late': original=replace(original,financials=tuple(replace(r,ann_date=date(2025,1,1)) for r in original.financials))
    elif case=='before-listing': original=replace(original,securities=tuple(replace(s,list_date=date(2025,1,1)) for s in original.securities))
    else: original=replace(original,financials=tuple(replace(r,values={k:v for k,v in r.values.items() if k!='money_cap'}) if r.table=='balance' else r for r in original.financials))
    result=preview(original)
    decisions=[d for d in result['decisions'] if d['table']=='balance'] if case=='witness' else result['decisions']
    assert all(d['reason']==reason for d in decisions)
    assert not any(r['table']=='balance' for r in result['requests']) if case=='witness' else result['requests']==[]


def test_dynamic_sixty_one_member_pool_and_invalid_limits():
    result=preview(data(61))
    assert result['poolCount']==61 and result['logicalRequestCount']==366
    for quarters,limit in ((0,100),(9,100),(True,100),(1,0),(1,1001),(1,True)):
        with pytest.raises(ValueError): preview(quarters=quarters,limit=limit)


def test_field_summary_does_not_mix_financial_ineligible_reports_into_target_coverage():
    original=data()
    financial=replace(original,financials=tuple(replace(r,comp_type='2') for r in original.financials))
    result=preview(financial)
    assert result['fieldCounts']['balance.fix_assets']=={}
    assert result['fieldCounts']['cashflow.c_paid_to_for_empl']=={}
    assert result['fieldCountsScope']=='eligible-core-decisions'


@pytest.mark.parametrize('company_type,reason',[('2','financial-report'),('5','company-type-conflict')])
def test_company_eligibility_agrees_across_all_visible_tables(company_type,reason):
    original=data(1)
    original=replace(original,financials=tuple(replace(r,comp_type=company_type) if r.table=='cashflow' else r for r in original.financials))
    result=preview(original)
    assert result['requests']==[]
    assert all(d['reason']==reason for d in result['decisions'])
    assert result['fieldCounts']['balance.fix_assets']=={}


@pytest.mark.parametrize('table',['income','balance','cashflow'])
def test_tied_visible_core_versions_with_different_announcement_metadata_are_not_chosen(table):
    original=data(1)
    source=next(r for r in original.financials if r.table==table and r.end_date==date(2024,6,30))
    other=replace(source,ann_date=date(2024,7,9),f_ann_date=date(2024,7,10),source_key='000-other-version')
    result=preview(replace(original,financials=(*original.financials,other)))
    decisions=[d for d in result['decisions'] if d['period']=='2024-06-30']
    assert all(d['reason']=='revision-conflict' for d in decisions)
    assert not any(r['params']['period']=='20240630' for r in result['requests'])
