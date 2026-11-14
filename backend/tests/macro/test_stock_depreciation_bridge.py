from dataclasses import asdict,replace
from datetime import date
from copy import deepcopy
import importlib
import pytest
from app.macro.enterprise import EnterpriseInputs,FinancialRecord,ReaFieldInput,_hash
from app.connectors.tushare.strategies import LEGACY_CASHFLOW_FIELDS

FIELDS=('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')


def fixture():
    values={k:None for k in LEGACY_CASHFLOW_FIELDS if k not in
        ('ts_code','end_date','ann_date','f_ann_date','report_type','comp_type','update_flag')}
    values.update(n_cashflow_act=100,c_pay_acq_const_fiolta=20)
    record=FinancialRecord('cashflow','600519.SH',date(2024,12,31),date(2025,3,1),date(2025,3,1),'1','1','0',values,'core.cashflow:1')
    row=values|dict(ts_code=record.symbol,end_date='2024-12-31',ann_date='2025-03-01',f_ann_date='2025-03-01',report_type='1',comp_type='1',update_flag='0',**dict(zip(FIELDS,('10','2','1'))))
    return EnterpriseInputs(universe_version='pool',symbols=('600519.SH',),securities=(),financials=(record,),industries=()),record,row


def run(inputs,rows):
    try:fn=importlib.import_module('app.macro.stock_depreciation_bridge').bridge_depreciation
    except ModuleNotFoundError:pytest.fail('depreciation witness bridge not implemented')
    return fn(inputs,rows,available_date=date(2026,11,9),contract_hash='a'*64)


def original(row):return ('raw.source_payload:4:row:1','b'*64,row)


def test_only_missing_fields_added_as_separate_source_bound_evidence_without_core_changes():
    i,r,row=fixture();before=deepcopy(i);result,audit=run(i,[original(row)])
    assert i==before and result.financials==i.financials
    assert len(result.rea_fields)==3 and audit['matchedFields']==3
    for field in result.rea_fields:
        assert field.source_key==r.source_key and field.source_hash==_hash(asdict(r))
        assert field.status=='matched' and field.available_date==date(2026,11,9)
        assert field.request_key.startswith('stock-depreciation:') and len(field.evidence_hash)==64


@pytest.mark.parametrize('change',['metadata','witness','missing-witness','ambiguous','other-symbol'])
def test_no_match_or_ambiguity_does_not_supply_data(change):
    i,_,row=fixture();rows=[original(row)]
    if change=='metadata':row['ann_date']='2025-03-02'
    elif change=='witness':row['n_cashflow_act']=101
    elif change=='missing-witness':row.pop('free_cashflow')
    elif change=='ambiguous':rows.append(('raw.source_payload:5:row:1','c'*64,deepcopy(row)))
    else:row['ts_code']='000001.SZ'
    result,audit=run(i,rows)
    assert result is i and not result.rea_fields and audit['matchedFields']==0


def test_null_zero_negative_missing_and_existing_fields_are_distinct():
    i,r,row=fixture();row[FIELDS[0]]=None;row[FIELDS[1]]=0;row[FIELDS[2]]=-1
    result,_=run(i,[original(row)])
    assert [(f.status,f.value) for f in result.rea_fields]==[('returned-null',None),('matched','0'),('invalid-value',None)]
    record=replace(r,values=r.values|{FIELDS[0]:None})
    i=replace(i,financials=(record,));row.pop(FIELDS[1])
    old=ReaFieldInput(record.source_key,_hash(asdict(record)),FIELDS[2],'matched','9','d'*64,date(2026,10,28),'old')
    result,_=run(replace(i,rea_fields=(old,)),[original(row)])
    assert result.rea_fields[0] is old
    assert [(f.field,f.status) for f in result.rea_fields]==[(FIELDS[2],'matched'),(FIELDS[1],'missing-field')]


def test_financial_company_and_wrong_scope_are_not_enriched():
    i,r,row=fixture();r=replace(r,comp_type='2');row['comp_type']='2'
    result,_=run(replace(i,financials=(r,)),[original(row)])
    assert not result.rea_fields
