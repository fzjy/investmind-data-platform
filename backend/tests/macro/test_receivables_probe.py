from backend.tests.macro.pg_evidence import evidence_fixture_transaction
from backend.tests.support.pg_snapshot import database_snapshot
import inspect
import json

import httpx
import pytest
from sqlalchemy import select,delete
from typer.testing import CliRunner

from app.cli.main import app
from app.macro.jobs.receivables_batch import execute_receivables_batch
from backend.tests.macro.receivables_samples import AS_OF,POLICY,full_inputs,response_for,alter_response
from backend.tests.macro.test_receivables_batch import setup_database,transport_for,run
from backend.tests.macro.test_enterprise_cli import database


def probe(factory,data,transport):
    assert 'probe' in inspect.signature(execute_receivables_batch).parameters, 'one-attempt probe not implemented'
    return execute_receivables_batch(factory,data,AS_OF,POLICY,quarters=8,request_limit=100,
        page_size=5000,token='controlled-token',transport=transport,probe=True)


def test_probe_one_actual_attempt_preserves_config_plan_and_batch_reuses_result(pg_schema_url):
    batch,store,models,url,engine,factory=setup_database(pg_schema_url)
    data=full_inputs();calls=[]
    try:
        first=probe(factory,data,transport_for(data,calls))
        assert first['operation']=='probe' and first['httpLimit']==1 and first['configuredHttpLimit']==100
        assert first['probeReady'] is True and first['responseCount']==first['newMatchedCount']==first['requestCount']==len(calls)==1
        assert first['pendingCount']==3 and first['status']=='partial'
        second=run(batch,factory,data,transport_for(data,calls))
        assert second['planFingerprint']==first['planFingerprint']
        assert second['requestCount']==3 and second['matchedCount']==4 and len(calls)==4
        assert second['httpLimit']==second['configuredHttpLimit']==100 and second['operation']=='batch'
    finally: engine.dispose()


@pytest.mark.parametrize('replay_only',[False,True])
def test_cached_or_replayed_result_does_not_claim_fresh_permission(replay_only,pg_schema_url):
    batch,store,models,url,engine,factory=setup_database(pg_schema_url)
    data=full_inputs();calls=[]
    try:
        run(batch,factory,data,transport_for(data,calls))
        if replay_only:
            with evidence_fixture_transaction(factory, models.ReceivableSupplement) as session:
                key=session.scalar(select(models.ReceivableSupplement.request_key))
                session.execute(delete(models.ReceivableSupplement).where(models.ReceivableSupplement.request_key==key))
        result=probe(factory,data,transport_for(data,calls))
        assert result['requestCount']==result['responseCount']==result['newMatchedCount']==0
        assert result['probeReady'] is False and result['matchedCount']==4 and len(calls)==4
    finally: engine.dispose()


@pytest.mark.parametrize('condition',['null','no-match','permission'])
def test_unusable_probe_cannot_authorize_bulk_collection(condition,pg_schema_url):
    batch,store,models,url,engine,factory=setup_database(pg_schema_url)
    data=full_inputs();calls=[]
    transport=transport_for(data,calls,value=None) if condition=='null' else transport_for(data,calls,status=403)
    if condition=='no-match':
        def handle(request):
            params=json.loads(request.content)['params'];calls.append(params)
            source=next(r for r in data.financials if r.table=='balance' and r.symbol==params['ts_code'] and r.end_date.strftime('%Y%m%d')==params['period'])
            response=alter_response(response_for(source),change={'money_cap':999})
            return httpx.Response(200,json={'code':0,'data':response.model_dump(mode='json')})
        transport=httpx.MockTransport(handle)
    try:
        result=probe(factory,data,transport)
        assert result['probeReady'] is False and result['newMatchedCount']==0
        assert result['requestCount']==len(calls)==1 and result['pendingCount']>=3
        assert result['responseCount']==(0 if condition=='permission' else 1)
        if condition=='permission': assert result['status']=='blocked'
    finally: engine.dispose()


def test_probe_cli_dry_run_uses_effective_one_without_token_or_writes(database,monkeypatch):
    db_engine,_=database;before=database_snapshot(db_engine)
    monkeypatch.delenv('DANG_TUSHARE_TOKEN',raising=False)
    monkeypatch.setenv('DANG_MACRO_CATCHUP_REQUEST_LIMIT','100')
    result=CliRunner().invoke(app,['probe-enterprise-receivables','--dry-run'])
    assert result.exit_code==0,result.stdout
    data=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
    assert data['httpLimit']==1 and data['configuredHttpLimit']==100 and data['operation']=='probe'
    assert data['probeReady'] is False and data['requestCount']==0 and data['readOnly'] is True
    assert database_snapshot(db_engine)==before
