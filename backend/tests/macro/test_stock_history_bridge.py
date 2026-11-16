from dataclasses import replace
from datetime import date, datetime, timezone
import importlib
import json, base64, hashlib

import pytest
from sqlalchemy import select
from app.db.models import IngestionBatch, SourcePayload
from app.macro.enterprise import EnterpriseInputs, FinancialRecord, _select, _hash
from backend.tests.test_financial_supply import Client, module as supply
from backend.tests.test_dividend_evidence_capture import source, db_state


def bridge():
    try:
        return importlib.import_module('app.macro.stock_history_bridge')
    except ModuleNotFoundError:
        pytest.fail('stock history bridge not implemented')


def inputs():
    return EnterpriseInputs('test', ('600519.SH',), (), (), ())


def prepared(new_pg_factory):
    f=source(new_pg_factory); supply().capture(f,Client(),execute=True)
    with f.begin() as s:
        for r in s.scalars(select(SourcePayload)):r.captured_at=datetime(2026,11,5,12,tzinfo=timezone.utc)
        for r in s.scalars(select(IngestionBatch).where(IngestionBatch.dataset==supply().JOB)):
            r.finished_at=datetime(2026,11,5,13,tzinfo=timezone.utc)
    return f


def test_disabled_never_reads_and_no_contract_is_identity(new_pg_factory):
    m=bridge(); data=inputs()
    assert m.load_stock_history_bridge(None,data,date(2026,11,7)) is data
    assert m.load_stock_history_bridge(source(new_pg_factory),data,date(2026,11,7),enabled=True) is data


def test_real_original_append_only_readonly_and_reentry(new_pg_factory):
    m=bridge();f=prepared(new_pg_factory);data=inputs();before=db_state(f)
    old=FinancialRecord('income','600519.SH',date(2024,12,31),None,None,'1','1','0',{'revenue':None},'core.income_statement:old')
    data=replace(data,financials=(old,));original_hash=_hash(data)
    result,audit=m.audit_stock_history_bridge(f,data,date(2026,11,7),enabled=True)
    assert result.financials[0] is old and len(result.financials)==6
    assert len({(r.table,r.symbol,r.end_date) for r in result.financials})==6
    assert db_state(f)==before and _hash(data)==original_hash
    assert audit['addedRecords']==5 and audit['requestCount']==0
    assert m.load_stock_history_bridge(f,result,date(2026,11,7),enabled=True) is result
    r=result.financials[1]
    assert r.source_key.startswith('raw.source_payload:') and r.amount_unit=='yuan'
    assert r.available_date==date(2026,11,6) and r.evidence_hash
    assert _select((r,),date(2026,10,28))[0] is None
    assert _select((r,),date(2026,11,7))[0] is r


@pytest.mark.parametrize('cutoff',[date(2026,10,28),date(2026,11,5)])
def test_old_cutoff_never_backfills(new_pg_factory, cutoff):
    m=bridge();data=inputs()
    assert m.load_stock_history_bridge(prepared(new_pg_factory),data,cutoff,enabled=True) is data


def test_scope_mismatch_no_bridge(new_pg_factory):
    m=bridge();data=replace(inputs(),symbols=('000001.SZ',))
    assert m.load_stock_history_bridge(prepared(new_pg_factory),data,date(2026,11,7),enabled=True) is data


@pytest.mark.parametrize('column',['captured_at','finished_at'])
def test_any_page_late_blocks_entire_contract(new_pg_factory, column):
    m=bridge();f=prepared(new_pg_factory);data=inputs()
    with f.begin() as s:
        model=SourcePayload if column=='captured_at' else IngestionBatch
        q=select(model)
        if model is IngestionBatch:q=q.where(model.dataset==supply().JOB)
        setattr(s.scalars(q).first(),column,datetime(2026,11,8,tzinfo=timezone.utc))
    assert m.load_stock_history_bridge(f,data,date(2026,11,7),enabled=True) is data


def test_original_tamper_is_not_silent_fallback(new_pg_factory):
    m=bridge();f=prepared(new_pg_factory)
    with f.begin() as s:
        r=s.scalars(select(SourcePayload)).first();r.content_hash='bad'
    with pytest.raises(ValueError):m.load_stock_history_bridge(f,inputs(),date(2026,11,7),enabled=True)


def test_announcement_ranking_and_conflict_not_capture_ranking(new_pg_factory):
    m=bridge();r=m.load_stock_history_bridge(prepared(new_pg_factory),inputs(),date(2026,11,7),enabled=True).financials[0]
    later=replace(r,ann_date=date(2025,3,2),f_ann_date=date(2025,3,2),values={'revenue':200},source_key=r.source_key+':later')
    assert _select((r,later),date(2026,11,7))[0] is later
    conflict=replace(later,values={'revenue':300},source_key=r.source_key+':conflict')
    assert _select((later,conflict),date(2026,11,7))==(None,'revision-conflict')
    assert _select((later,conflict),date(2025,5,1))==(None,'missing-report')


def test_loader_has_explicit_policy_keyword():
    import inspect
    from app.macro.enterprise_contract import enterprise_loader
    assert inspect.signature(enterprise_loader).parameters['stock_history_enabled'].default is None


class RowsClient(Client):
    def __init__(self, change):
        super().__init__(); self.change=change

    def call(self, api, params, fields):
        from app.connectors.tushare.contracts import TushareResponse
        rows=[]
        row=dict.fromkeys(fields)
        row.update(ts_code=params['ts_code'],end_date='20241231',ann_date='20250301',
            f_ann_date='20250301',report_type='1',comp_type='1',update_flag='0',revenue=100)
        rows=self.change(api,row)
        if getattr(self,'paging',False) and api=='cashflow' and params.get('offset'):
            rows=[]
        data=dict(fields=list(fields),items=[[r[k] for k in fields] for r in rows])
        if getattr(self,'paging',False) and api=='cashflow':
            data['has_more']=not bool(params.get('offset'))
        body=json.dumps({'code':0,'data':data}).encode()
        self.callback(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),
            bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
        return TushareResponse.model_validate(data)


def test_first_empty_page_is_verified_and_late_completion_blocks_siblings(new_pg_factory):
    m=bridge();f=source(new_pg_factory)
    supply().capture(f,RowsClient(lambda api,row:[] if api=='cashflow' else [row]),execute=True)
    with f.begin() as s:
        for r in s.scalars(select(SourcePayload)):r.captured_at=datetime(2026,11,5,tzinfo=timezone.utc)
        for b in s.scalars(select(IngestionBatch).where(IngestionBatch.dataset==supply().JOB)):
            b.finished_at=datetime(2026,11,8 if b.row_count==0 else 5,tzinfo=timezone.utc)
    data=inputs()
    assert m.load_stock_history_bridge(f,data,date(2026,11,7),enabled=True) is data
    result,audit=m.audit_stock_history_bridge(f,data,date(2026,11,10),enabled=True)
    assert audit['pageCount']==3 and audit['blockedTupleCount']==0
    assert {r.table for r in result.financials}=={'income','balance'}
    assert all(r.available_date==date(2026,11,9) for r in result.financials)


def test_empty_tail_late_completion_blocks_nonempty_sibling_pages(new_pg_factory):
    m=bridge();f=source(new_pg_factory);client=RowsClient(lambda api,row:[row]);client.paging=True
    assert supply().capture(f,client,execute=True)['status']=='completed'
    with f.begin() as s:
        for r in s.scalars(select(SourcePayload)):r.captured_at=datetime(2026,11,5,tzinfo=timezone.utc)
        for b in s.scalars(select(IngestionBatch).where(IngestionBatch.dataset==supply().JOB)):
            b.finished_at=datetime(2026,11,8 if b.row_count==0 else 5,tzinfo=timezone.utc)
    data=inputs()
    assert m.load_stock_history_bridge(f,data,date(2026,11,7),enabled=True) is data
    result,audit=m.audit_stock_history_bridge(f,data,date(2026,11,10),enabled=True)
    assert result.financials and audit['pageCount']==4
    assert all(r.available_date==date(2026,11,9) for r in result.financials)


@pytest.mark.parametrize('change',[
    {'end_date':'20240831'}, {'report_type':'6'}, {'ann_date':'20240101'},
    {'comp_type':None}, {'update_flag':'bad'},
])
def test_invalid_raw_metadata_never_enters_macroeconomic_inputs(new_pg_factory, change):
    m=bridge();f=source(new_pg_factory)
    supply().capture(f,RowsClient(lambda api,row:[row|change]),execute=True)
    data=inputs()
    assert m.load_stock_history_bridge(f,data,date(2099,1,1),enabled=True) is data


def test_real_same_version_raw_conflict_and_capture_guard(new_pg_factory):
    m=bridge();f=source(new_pg_factory)
    supply().capture(f,RowsClient(lambda api,row:[row,row|{'revenue':200}] if api=='income' else [row]),execute=True)
    result=m.load_stock_history_bridge(f,inputs(),date(2099,1,1),enabled=True)
    income=tuple(r for r in result.financials if r.table=='income')
    assert len(income)==2
    assert _select(income,date(2099,1,1))==(None,'revision-conflict')
    assert _select(income,date(2026,1,1))==(None,'missing-report')


@pytest.mark.parametrize('announcement',['20250301','20250401'])
def test_invalid_same_or_newer_revision_blocks_older_tuple(new_pg_factory, announcement):
    m=bridge();f=source(new_pg_factory)
    supply().capture(f,RowsClient(lambda api,row:[row,row|{'ann_date':announcement,
        'f_ann_date':announcement,'comp_type':None,'revenue':999}] if api=='income' else [row]),execute=True)
    result,audit=m.audit_stock_history_bridge(f,inputs(),date(2099,1,1),enabled=True)
    income=tuple(r for r in result.financials if r.table=='income')
    assert _select(income,date(2099,1,1))==(None,'missing-report')
    assert len(result.financials)==2
    assert audit['blockedTupleCount']==1


def test_nonstandard_period_does_not_quarantine_valid_quarter(new_pg_factory):
    m=bridge();f=source(new_pg_factory)
    supply().capture(f,RowsClient(lambda api,row:[row,row|{'end_date':'20241130',
        'comp_type':None}] if api=='income' else [row]),execute=True)
    result,audit=m.audit_stock_history_bridge(f,inputs(),date(2099,1,1),enabled=True)
    assert len([r for r in result.financials if r.table=='income'])==1
    assert audit['blockedTupleCount']==0


def test_loader_adds_bridge_only_after_existing_supplements(monkeypatch):
    m=bridge()
    import app.macro.enterprise_repository as repository
    import app.macro.enterprise_receivables as receivables
    import app.macro.enterprise_rea_inputs as rea
    import app.macro.enterprise_contract as contract
    from types import SimpleNamespace
    events=[]
    monkeypatch.setattr(repository,'resolve_enterprise_selection',lambda *_:None)
    monkeypatch.setattr(repository,'load_enterprise_inputs',lambda *_:inputs())
    monkeypatch.setattr(repository,'load_enterprise_loss_inputs',lambda f,i,*_:i)
    monkeypatch.setattr(receivables,'load_enterprise_receivables',lambda f,i,*a,**kw:(events.append('supplement') or i))
    monkeypatch.setattr(rea,'load_enterprise_rea_inputs',lambda f,i,*a,**kw:(events.append('rea') or i))
    monkeypatch.setattr(m,'load_stock_history_bridge',lambda f,i,*a,**kw:(events.append('bridge') or i))
    class End(Exception):pass
    def stop(*a,**kw):raise End()
    monkeypatch.setattr(contract,'public_enterprise_analysis',stop)
    settings=SimpleNamespace(macro_enterprise_min_coverage=80,macro_enterprise_min_samples=5,
        macro_enterprise_max_quarters=40,sync_timezone='Asia/Shanghai',tushare_page_size=100)
    with pytest.raises(End):contract.enterprise_loader(None,settings,stock_history_enabled=False)(date(2026,11,7))
    assert events==['supplement','supplement','rea']
    events.clear()
    with pytest.raises(End):contract.enterprise_loader(None,settings,stock_history_enabled=True)(date(2026,11,7))
    assert events==['supplement','supplement','rea','bridge']
    seen=[]
    monkeypatch.setattr(m,'load_stock_history_bridge',lambda f,i,a,**kw:(seen.append(a) or i))
    instant=datetime(2026,11,8,17,tzinfo=timezone.utc)  # 上海已9日，UTC仍8日。
    with pytest.raises(End):contract.enterprise_loader(None,settings,stock_history_enabled=True)(instant)
    assert seen==[instant], 'source visibility needs actual UTC instant, not next Shanghai date'
