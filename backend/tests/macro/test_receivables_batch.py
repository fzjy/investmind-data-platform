from dataclasses import replace
import importlib
import importlib.util
import json

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db.base import Base
from app.db.session import create_engine_and_session
from backend.tests.macro.receivables_samples import AS_OF,POLICY,full_inputs,response_for


def modules():
    if importlib.util.find_spec('app.macro.jobs.receivables_batch') is None:
        pytest.fail('durable receivables executor not implemented')
    return (importlib.import_module('app.macro.jobs.receivables_batch'),
        importlib.import_module('app.macro.receivables_store'),
        importlib.import_module('app.db.models.enterprise_receivables'))


def setup_database(tmp_path):
    batch,store,models=modules()
    url=f'sqlite:///{(tmp_path/"supplement.sqlite").as_posix()}'
    engine,factory=create_engine_and_session(url)
    Base.metadata.create_all(engine)
    return batch,store,models,url,engine,factory


def transport_for(data,requests,*,value=123,status=200):
    def handle(request):
        payload=json.loads(request.content)
        assert payload['api_name']=='balancesheet'
        assert set(payload['params'])=={'ts_code','period','report_type'}
        assert payload['params']['report_type']=='1'
        requests.append(payload['params'])
        if status!=200: return httpx.Response(status,json={'msg':'private-token private-host'})
        source=next(r for r in data.financials if r.table=='balance' and r.symbol==payload['params']['ts_code']
            and r.end_date.strftime('%Y%m%d')==payload['params']['period'])
        response=response_for(source,value)
        return httpx.Response(200,json={'code':0,'data':response.model_dump(mode='json')})
    return httpx.MockTransport(handle)


def run(batch,factory,data,transport,limit=100,page_size=5000):
    return batch.execute_receivables_batch(factory,data,AS_OF,POLICY,quarters=8,
        request_limit=limit,page_size=page_size,token='controlled-token',transport=transport)


def test_actual_sql_and_protocol_budget_resume_across_engines_then_zero_http(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    try:
        first=run(batch,factory,data,transport_for(data,calls),limit=1)
        assert (first['status'],first['requestCount'],first['matchedCount'],first['pendingCount'])==('partial',1,1,3)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(models.ReceivableAttempt))==1
            assert session.scalar(select(func.count()).select_from(models.ReceivableResponse))==1
            assert session.scalar(select(func.count()).select_from(models.ReceivableSupplement))==1
        engine.dispose()
        engine,factory=create_engine_and_session(url)
        second=run(batch,factory,data,transport_for(data,calls),limit=100)
        assert (second['status'],second['requestCount'],second['matchedCount'],second['pendingCount'])==('completed',3,4,0)
        third=run(batch,factory,data,transport_for(data,calls),limit=2)
        assert third['status']=='completed' and third['requestCount']==0 and len(calls)==4
        assert third['runId']!=second['runId']
    finally: engine.dispose()


def test_raw_response_survives_interruption_and_replays_without_refetch(tmp_path,monkeypatch):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    original=store.ReceivablesStore.save_result
    def interrupted(*args,**kwargs): raise KeyboardInterrupt('controlled shutdown boundary')
    monkeypatch.setattr(store.ReceivablesStore,'save_result',interrupted)
    try:
        with pytest.raises(KeyboardInterrupt): run(batch,factory,data,transport_for(data,calls),limit=1)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(models.ReceivableResponse))==1
            assert session.scalar(select(func.count()).select_from(models.ReceivableSupplement))==0
        monkeypatch.setattr(store.ReceivablesStore,'save_result',original)
        engine.dispose(); engine,factory=create_engine_and_session(url)
        recovered=run(batch,factory,data,transport_for(data,calls),limit=3)
        assert recovered['requestCount']==3 and recovered['matchedCount']==4 and len(calls)==4
    finally: engine.dispose()


@pytest.mark.parametrize('value,page_size,reason',[(None,5000,'returned-null'),(123,1,'truncated')])
def test_terminal_unavailability_is_not_zero_or_blindly_retried(tmp_path,value,page_size,reason):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    try:
        first=run(batch,factory,data,transport_for(data,calls,value=value),page_size=page_size)
        assert first['status']=='completed-with-gaps' and first['unavailableReasons']=={reason:4}
        assert first['matchedCount']==0 and first['processingCompleted'] is True
        second=run(batch,factory,data,transport_for(data,calls),page_size=page_size)
        assert second['requestCount']==0 and len(calls)==4
        with factory() as session:
            assert all(row.value is None for row in session.scalars(select(models.ReceivableSupplement)))
    finally: engine.dispose()


@pytest.mark.parametrize('status',[403,429,500])
def test_failed_physical_attempt_is_budgeted_saved_and_stops_without_hidden_retry(tmp_path,status):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    try:
        result=run(batch,factory,data,transport_for(data,calls,status=status))
        assert result['status']=='blocked' and result['requestCount']==len(calls)==1
        assert result['pendingCount']==4
        with factory() as session:
            attempt=session.scalar(select(models.ReceivableAttempt))
            assert attempt.state=='failed' and attempt.error_code.startswith('Tushare')
            assert 'private' not in attempt.error_code
        assert 'private' not in json.dumps(result)
    finally: engine.dispose()


def test_network_failure_is_counted_and_next_manual_batch_can_resume(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    def broken(request):
        calls.append(request)
        raise httpx.ConnectError('private-host',request=request)
    try:
        failed=run(batch,factory,data,httpx.MockTransport(broken),limit=1)
        assert failed['requestCount']==len(calls)==1 and failed['status']=='blocked'
        recovered=run(batch,factory,data,transport_for(data,[]),limit=4)
        assert recovered['requestCount']==4 and recovered['matchedCount']==4
    finally: engine.dispose()


def test_reservation_failure_prevents_any_http(tmp_path,monkeypatch):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    def broken(*args,**kwargs): raise ValueError('ENTERPRISE_RESERVATION_FAILED')
    monkeypatch.setattr(store.ReceivablesStore,'reserve',broken)
    try:
        result=run(batch,factory,data,transport_for(data,calls))
        assert result['status']=='blocked' and not calls
    finally: engine.dispose()


def test_core_revision_creates_new_identity_and_preserves_old_facts(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    try:
        assert run(batch,factory,data,transport_for(data,calls))['matchedCount']==4
        changed=replace(data,financials=tuple(replace(r,values={**r.values,'money_cap':999})
            if r.table=='balance' and r.symbol=='000001.SZ' and r.end_date.year==2024 else r for r in data.financials))
        result=run(batch,factory,changed,transport_for(changed,calls))
        assert result['requestCount']==1 and result['matchedCount']==4 and len(calls)==5
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(models.ReceivableSupplement))==5
    finally: engine.dispose()


def test_corrupt_response_blocks_all_requests_without_overwrite_or_refetch(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    try:
        run(batch,factory,data,transport_for(data,calls),limit=1)
        with factory.begin() as session:
            row=session.scalar(select(models.ReceivableResponse))
            row.response_hash='0'*64
        result=run(batch,factory,data,transport_for(data,calls))
        assert result['status']=='blocked' and result['requestCount']==0 and len(calls)==1
        assert result['errorCode']=='ENTERPRISE_EVIDENCE_INVALID'
    finally: engine.dispose()


def test_unfinished_reserved_attempt_is_not_mistaken_for_completed_response(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs(); calls=[]
    def interrupted(request):
        calls.append(request)
        raise KeyboardInterrupt('controlled unknown delivery boundary')
    try:
        with pytest.raises(KeyboardInterrupt): run(batch,factory,data,httpx.MockTransport(interrupted),limit=1)
        with factory() as session:
            assert session.scalar(select(models.ReceivableAttempt)).state=='reserved'
        engine.dispose();engine,factory=create_engine_and_session(url)
        resumed=run(batch,factory,data,transport_for(data,[]),limit=4)
        assert resumed['requestCount']==4 and resumed['matchedCount']==4
    finally: engine.dispose()


def test_page_interpretation_policy_change_is_explicitly_blocked_without_http(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs();calls=[]
    try:
        run(batch,factory,data,transport_for(data,calls))
        result=run(batch,factory,data,transport_for(data,calls),page_size=1)
        assert result['status']=='blocked' and result['errorCode']=='ENTERPRISE_PAGE_POLICY_CHANGED'
        assert result['requestCount']==0 and len(calls)==4
    finally: engine.dispose()


def test_later_invalid_response_does_not_erase_earlier_committed_progress(tmp_path):
    batch,store,models,url,engine,factory=setup_database(tmp_path)
    data=full_inputs();calls=[]
    def handle(request):
        params=json.loads(request.content)['params'];calls.append(params)
        source=next(r for r in data.financials if r.table=='balance' and r.symbol==params['ts_code'] and r.end_date.strftime('%Y%m%d')==params['period'])
        response=response_for(source)
        if len(calls)==2:
            response=response.model_copy(update={'fields':(*response.fields,'accounts_receiv'),'items':((*response.items[0],999),)})
        return httpx.Response(200,json={'code':0,'data':response.model_dump(mode='json')})
    try:
        result=run(batch,factory,data,httpx.MockTransport(handle))
        assert result['status']=='blocked' and result['requestCount']==len(calls)==2
        assert result['matchedCount']==1 and result['pendingCount']==3
        assert result['unverifiedCount']==1 and result['processingCompleted'] is False
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(models.ReceivableSupplement))==1
    finally: engine.dispose()
