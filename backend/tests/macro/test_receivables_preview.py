from dataclasses import replace
from datetime import date
import importlib
import importlib.util

import pytest

from app.macro.enterprise import EnterprisePolicy
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from backend.tests.macro.test_enterprise import inputs


def module():
    if importlib.util.find_spec('app.macro.receivables_preview') is None:
        pytest.fail('receivables readonly planner not implemented')
    return importlib.import_module('app.macro.receivables_preview')


def preview(data=None,**kwargs):
    return module().preview_receivables(data or inputs(),date(2024,5,31),EnterprisePolicy(min_samples=1,min_coverage=50),kwargs.pop('quarters',8),kwargs.pop('request_limit',100))


def test_exact_periods_witness_fields_budget_and_no_collection_claim():
    fields=tuple(FIELD_REGISTRY[Dataset.BALANCE_SHEET])
    result=preview()
    assert result['outputPeriods']==['2024-03-31']
    assert result['balancePeriods']==['2023-03-31','2024-03-31']
    assert result['logicalRequestCount']==4 and result['physicalHttpExecuted']==0
    assert result['persistenceImplemented'] is False and result['permissionsVerified'] is False
    for r in result['requests']:
        assert r['apiName']=='balancesheet'
        assert set(r['params'])=={'ts_code','period','report_type'}
        assert r['params']['report_type']=='1'
        assert r['fields']==list((*fields,'accounts_receiv'))
        assert len(r['requestKey'])==len(r['coreBinding']['valueHash'])==64
    assert FIELD_REGISTRY[Dataset.BALANCE_SHEET]==fields


def test_reordering_and_next_day_without_new_quarter_keep_request_identity():
    data=inputs()
    first=preview(data)
    reordered=preview(replace(data,financials=tuple(reversed(data.financials)),symbols=tuple(reversed(data.symbols))))
    tomorrow=module().preview_receivables(data,date(2024,6,1),EnterprisePolicy(min_samples=1,min_coverage=50),8,100)
    assert first['planFingerprint']==reordered['planFingerprint']==tomorrow['planFingerprint']
    changed=replace(data,financials=tuple(replace(r,values={**r.values,'inventories':999}) if r.table=='balance' and r.end_date.year==2024 and r.symbol=='000001.SZ' else r for r in data.financials))
    before={r['params']['ts_code']+r['params']['period']:r['requestKey'] for r in first['requests']}
    after={r['params']['ts_code']+r['params']['period']:r['requestKey'] for r in preview(changed)['requests']}
    assert len([k for k in before if before[k]!=after[k]])==1


@pytest.mark.parametrize('value,reason',[(0,'existing-finite-field'),(None,'existing-null-field'),(True,'invalid-existing-field'),('NaN','invalid-existing-field')])
def test_existing_zero_null_or_invalid_is_not_blindly_requested_again(value,reason):
    data=inputs()
    data=replace(data,financials=tuple(replace(r,values={**r.values,'accounts_receiv':value}) if r.table=='balance' and r.end_date.year==2024 and r.symbol=='000001.SZ' else r for r in data.financials))
    result=preview(data)
    assert result['logicalRequestCount']==3
    assert next(d for d in result['decisions'] if d['symbol']=='000001.SZ' and d['period']=='2024-03-31')['reason']==reason


def test_unknown_industry_and_core_revision_conflict_are_kept_as_decisions_not_fetches():
    data=inputs(); original=next(r for r in data.financials if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024)
    conflict=replace(original,values={'inventories':999},source_key='conflict')
    result=preview(replace(data,financials=(*data.financials,conflict)))
    assert len(result['decisions'])==4
    assert next(d for d in result['decisions'] if d['symbol']=='000001.SZ' and d['period']=='2024-03-31')['reason']=='revision-conflict'
    unknown=preview(replace(data,industries=tuple(i for i in data.industries if i.symbol!='000002.SZ')))
    assert len(unknown['decisions'])==4
    assert all(d['reason']=='unknown-or-overlapping-industry' for d in unknown['decisions'] if d['symbol']=='000002.SZ')


def test_batch_is_only_logical_candidate_subset_not_physical_budget_claim():
    result=preview(request_limit=2)
    assert result['logicalRequestCount']==4 and len(result['nextBatchCandidates'])==2
    assert result['physicalHttpLimit']==2
    assert result['requiresPaginationOrTruncationCheck'] is True
    assert result['physicalBudgetEnforced'] is False
    assert result['planFingerprint']!=preview(request_limit=100)['planFingerprint']
    assert result['requests'][0]['requestKey']==preview(request_limit=100)['requests'][0]['requestKey']


def test_target_binding_is_not_claimed_verified_before_supplier_response():
    assert all(r.get('coreBindingStatus')=='requires-response-witness-match' for r in preview()['requests'])


@pytest.mark.parametrize('value,revised',[(0,999),(None,None)])
def test_skipped_existing_field_decision_tracks_core_revision(value,revised):
    data=inputs()
    data=replace(data,financials=tuple(replace(r,values={**r.values,'accounts_receiv':value})
        if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024 else r for r in data.financials))
    changed=replace(data,financials=tuple(replace(r,values={**r.values,'accounts_receiv':revised},
        source_key=r.source_key+':revised',ann_date=date(2024,5,1))
        if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024 else r for r in data.financials))
    first,second=preview(data),preview(changed)
    assert first['outputPeriods']==second['outputPeriods']
    assert first['requests']==second['requests']
    assert first['planFingerprint']!=second['planFingerprint']
    old=next(d for d in first['decisions'] if d['symbol']=='000001.SZ' and d['period']=='2024-03-31')
    new=next(d for d in second['decisions'] if d['symbol']=='000001.SZ' and d['period']=='2024-03-31')
    assert old['coreBinding']!=new['coreBinding']
    assert 'accounts_receiv' not in new['coreBinding']


def test_conflict_decision_tracks_source_evidence_without_selecting_a_row():
    data=inputs()
    original=next(r for r in data.financials if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024)
    conflict=replace(original,values={'inventories':999},source_key='conflict')
    first=preview(replace(data,financials=(*data.financials,conflict)))
    second=preview(replace(data,financials=(*data.financials,replace(conflict,values={'inventories':888}))))
    assert first['outputPeriods']==second['outputPeriods'] and first['requests']==second['requests']
    assert first['planFingerprint']!=second['planFingerprint']
    decision=next(d for d in second['decisions'] if d['symbol']=='000001.SZ' and d['period']=='2024-03-31')
    assert decision['reason']=='revision-conflict' and decision['coreBinding'] is None
    assert len(decision['sourceEvidenceHash'])==64


def test_window_only_adds_required_year_ago_balances_not_missing_calendar_quarters():
    data=inputs(); extra=[]
    for row in data.financials:
        if row.end_date.year==2023:
            for year in (2021,2022):
                extra.append(replace(row,end_date=date(year,3,31),ann_date=date(year,4,30),source_key=f'{row.source_key}:{year}'))
    result=preview(replace(data,financials=(*data.financials,*extra)),quarters=2)
    assert result['outputPeriods']==['2023-03-31','2024-03-31']
    assert result['balancePeriods']==['2022-03-31','2023-03-31','2024-03-31']
    assert result['logicalRequestCount']==6


@pytest.mark.parametrize('condition,reason',[
    ('unknown-listing','unknown-listing-date'),('missing-core','core-version-missing'),
    ('future-core','core-version-missing'),('financial','financial-report'),
    ('overlap','unknown-or-overlapping-industry'),('before-listing','before-listing')])
def test_non_fetch_conditions_are_explicit_and_cannot_expand_universe(condition,reason):
    data=inputs(); symbol='000002.SZ'
    if condition=='unknown-listing':
        data=replace(data,securities=tuple(replace(s,list_date=None) if s.symbol==symbol else s for s in data.securities))
    elif condition=='before-listing':
        data=replace(data,securities=tuple(replace(s,list_date=date(2023,12,1)) if s.symbol==symbol else s for s in data.securities))
    elif condition=='missing-core':
        data=replace(data,financials=tuple(r for r in data.financials if not(r.symbol==symbol and r.table=='balance' and r.end_date.year==2024)))
    elif condition=='future-core':
        data=replace(data,financials=tuple(replace(r,ann_date=date(2027,1,1)) if r.symbol==symbol and r.table=='balance' and r.end_date.year==2024 else r for r in data.financials))
    elif condition=='financial':
        data=replace(data,financials=tuple(replace(r,comp_type='2') if r.symbol==symbol else r for r in data.financials))
    else:
        data=replace(data,industries=(*data.industries,next(i for i in data.industries if i.symbol==symbol)))
    result=preview(data)
    assert len(result['decisions'])==4
    assert any(d['symbol']==symbol and d['reason']==reason for d in result['decisions'])
    blocked_keys={(d['symbol'],d['period'].replace('-','')) for d in result['decisions'] if d['reason']!='supplement-required'}
    assert not any((r['params']['ts_code'],r['params']['period']) in blocked_keys for r in result['requests'])


@pytest.mark.parametrize('quarters,limit',[(0,100),(9,100),(True,100),(8,0),(8,True)])
def test_invalid_bounds_fail_closed(quarters,limit):
    with pytest.raises(ValueError): preview(quarters=quarters,request_limit=limit)
