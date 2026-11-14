"""订单补充：真实SQL持久层、受控供应商协议，旧应收空间隔离。"""
from dataclasses import replace
import importlib
import importlib.util
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.db.base import Base
from app.db.session import create_engine_and_session
from app.macro.receivables_preview import preview_receivables, receivable_request
from backend.tests.macro.receivables_samples import AS_OF, POLICY, full_inputs, response_for, alter_response
from backend.tests.macro.test_enterprise_cli import database as enterprise_database


def implementation():
    assert importlib.util.find_spec('app.macro.balance_supplement'), 'order supplement not implemented'
    return importlib.import_module('app.macro.balance_supplement')


def order_response(source, value=123):
    return alter_response(response_for(source),remove=['accounts_receiv'],change={'contract_liab':value})


def order_transport(data, calls, value=123):
    def handle(request):
        payload=json.loads(request.content)
        assert payload['api_name']=='balancesheet'
        assert payload['fields'].split(',')[-1]=='contract_liab'
        assert 'accounts_receiv' not in payload['fields'].split(',')
        assert set(payload['params'])=={'ts_code','period','report_type'}
        assert payload['params']['report_type']=='1'
        calls.append(payload['params'])
        source=next(r for r in data.financials if r.table=='balance' and r.symbol==payload['params']['ts_code']
            and r.end_date.strftime('%Y%m%d')==payload['params']['period'])
        return httpx.Response(200,json={'code':0,'data':order_response(source,value).model_dump(mode='json')})
    return httpx.MockTransport(handle)


def database(tmp_path):
    profile=implementation()
    models=importlib.import_module('app.db.models.enterprise_orders')
    engine,factory=create_engine_and_session(f'sqlite:///{(tmp_path/"orders.sqlite").as_posix()}')
    Base.metadata.create_all(engine)
    return profile,models,engine,factory


def execute(factory,data,calls,*,limit=100,value=123,probe=False,page_size=5000):
    profile=implementation()
    from app.macro.jobs.receivables_batch import execute_receivables_batch
    return execute_receivables_batch(factory,data,AS_OF,POLICY,quarters=8,request_limit=limit,
        page_size=page_size,token='controlled-token',transport=order_transport(data,calls,value),
        probe=probe,target=profile.ORDERS)


def test_closed_order_plan_has_separate_identity_and_keeps_old_preview_exact():
    profile=implementation(); data=full_inputs()
    old=preview_receivables(data,AS_OF,POLICY,8,100)
    order=preview_receivables(data,AS_OF,POLICY,8,100,target=profile.ORDERS)
    assert order['targetField']=='contract_liab' and order['logicalRequestCount']==4
    assert order['physicalHttpExecuted']==0 and order['readOnly']
    assert order['planFingerprint']!=old['planFingerprint']
    assert not set(r['requestKey'] for r in old['requests']) & set(r['requestKey'] for r in order['requests'])
    assert preview_receivables(data,AS_OF,POLICY,8,100)==old
    assert preview_receivables(data,AS_OF,POLICY,8,100,target=profile.RECEIVABLES)==old
    with pytest.raises(ValueError): preview_receivables(data,AS_OF,POLICY,8,100,target='contract_liab')


def test_order_sql_budget_new_engine_resume_then_zero_http_and_no_old_ar_rows(tmp_path):
    profile,models,engine,factory=database(tmp_path);data=full_inputs();calls=[];url=str(engine.url)
    try:
        first=execute(factory,data,calls,limit=1,probe=True)
        assert first['probeReady'] and first['requestCount']==1 and first['pendingCount']==3
        engine.dispose();engine,factory=create_engine_and_session(url)
        second=execute(factory,data,calls,limit=3)
        assert second['status']=='completed' and second['matchedCount']==4 and second['requestCount']==3
        third=execute(factory,data,calls,probe=True)
        assert third['requestCount']==0 and not third['probeReady'] and len(calls)==4
        from app.db.models.enterprise_receivables import ReceivableAttempt,ReceivableResponse,ReceivableSupplement
        with factory() as session:
            for model in (models.OrderAttempt,models.OrderResponse,models.OrderSupplement):
                assert session.scalar(select(func.count()).select_from(model))==4
            for model in (ReceivableAttempt,ReceivableResponse,ReceivableSupplement):
                assert session.scalar(select(func.count()).select_from(model))==0
    finally: engine.dispose()


def test_order_readonly_loader_separate_destination_no_source_mutation_and_new_revision_not_reused(tmp_path):
    profile,models,engine,factory=database(tmp_path);data=full_inputs();calls=[]
    from app.macro.enterprise_receivables import load_enterprise_receivables
    try:
        execute(factory,data,calls)
        path=tmp_path/'orders.sqlite';before=path.read_bytes()
        loaded=load_enterprise_receivables(factory,data,AS_OF,POLICY,page_size=5000,timezone='Asia/Shanghai',target=profile.ORDERS)
        assert len(loaded.contract_liabilities)==4 and loaded.receivables is None
        assert loaded.financials==data.financials and path.read_bytes()==before
        assert all(r.available_date>AS_OF for r in loaded.contract_liabilities)
        revision=replace(data,financials=tuple(replace(r,values={**r.values,'total_assets':9999}) if r.table=='balance' else r for r in data.financials))
        changed=load_enterprise_receivables(factory,revision,AS_OF,POLICY,page_size=5000,timezone='Asia/Shanghai',target=profile.ORDERS)
        assert changed.contract_liabilities==() and path.read_bytes()==before
    finally: engine.dispose()


def test_order_cli_dry_run_and_missing_schema_do_not_mutate_database(enterprise_database,monkeypatch):
    from typer.testing import CliRunner
    from app.cli.main import app
    from sqlalchemy import text
    path,factory=enterprise_database
    with factory.begin() as session:
        for table in ('enterprise_order_supplement','enterprise_order_response','enterprise_order_attempt'):
            session.execute(text(f'DROP TABLE {table}'))
    before=path.read_bytes()
    result=CliRunner().invoke(app,['sync-enterprise-orders','--dry-run'])
    assert result.exit_code==0,result.stdout
    body=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
    assert body['requestCount']==0 and body['schemaReady'] is False and len(body['missingTables'])==3
    assert path.read_bytes()==before
    assert CliRunner().invoke(app,['sync-enterprise-orders']).exit_code==2
    assert path.read_bytes()==before


@pytest.mark.parametrize('value,page_size,reason',[(None,5000,'returned-null'),(123,1,'truncated')])
def test_order_unavailable_is_sealed_not_zero_and_not_refetched(tmp_path,value,page_size,reason):
    profile,models,engine,factory=database(tmp_path);data=full_inputs();calls=[]
    try:
        first=execute(factory,data,calls,value=value,page_size=page_size)
        assert first['status']=='completed-with-gaps' and first['unavailableReasons']=={reason:4}
        assert execute(factory,data,calls,page_size=page_size)['requestCount']==0 and len(calls)==4
    finally: engine.dispose()


def test_order_raw_commit_interruption_recovers_without_duplicate_http(tmp_path,monkeypatch):
    profile,models,engine,factory=database(tmp_path);data=full_inputs();calls=[]
    from app.macro.receivables_store import ReceivablesStore
    original=ReceivablesStore.save_result
    def stop(*args,**kwargs): raise KeyboardInterrupt('controlled interruption after raw commit')
    monkeypatch.setattr(ReceivablesStore,'save_result',stop)
    try:
        with pytest.raises(KeyboardInterrupt): execute(factory,data,calls,limit=1)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(models.OrderResponse))==1
            assert session.scalar(select(func.count()).select_from(models.OrderSupplement))==0
        monkeypatch.setattr(ReceivablesStore,'save_result',original)
        result=execute(factory,data,calls,limit=3)
        assert result['matchedCount']==4 and result['requestCount']==3 and len(calls)==4
    finally: engine.dispose()


def test_order_witness_is_bound_to_original_values_and_page_policy(tmp_path):
    profile,models,engine,factory=database(tmp_path);data=full_inputs();calls=[]
    from app.macro.receivables_store import ReceivablesStore
    from app.macro.receivables_match import match_receivable
    try:
        request=preview_receivables(data,AS_OF,POLICY,8,100,target=profile.ORDERS)['requests'][0]
        source=next(r for r in data.financials if r.source_key==request['coreBinding']['sourceKey'])
        assert match_receivable(request,source,order_response(source),5000,target=profile.ORDERS)=={'status':'matched','value':'123'}
        changed=alter_response(order_response(source),change={'total_assets':9999})
        assert match_receivable(request,source,changed,5000,target=profile.ORDERS)['status']=='no-match'
        execute(factory,data,calls,limit=1)
        with pytest.raises(ValueError,match='PAGE_POLICY_CHANGED'):
            ReceivablesStore(factory,target=profile.ORDERS).recover(request,source,1)
        with factory.begin() as session:
            row=session.get(models.OrderResponse,session.scalar(select(models.OrderResponse.attempt_id)))
            row.response_hash='0'*64
        blocked=execute(factory,data,calls)
        assert blocked['status']=='blocked' and blocked['requestCount']==0 and len(calls)==1
    finally: engine.dispose()
