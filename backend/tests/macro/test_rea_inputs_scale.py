from backend.tests.macro.pg_evidence import evidence_fixture_transaction
"""接近当前981候选规模的真实持久恢复；无供应商HTTP。"""
from time import perf_counter

from sqlalchemy import insert

from app.macro.jobs.rea_inputs_batch import read_rea_progress
from app.macro.rea_input_match import match_rea_inputs
from app.macro import rea_input_store as module
from app.macro.enterprise import _hash
from backend.tests.macro.test_rea_input_store import database, response_for
from backend.tests.macro.test_rea_input_preview import data, preview


def test_large_durable_progress_is_linear_in_snapshot_decodes(database,monkeypatch):
    from datetime import UTC, datetime
    _,models,_,_,factory=database
    inputs=data(164)
    plan=preview(inputs,limit=100)
    assert plan['poolCount']==164 and plan['logicalRequestCount']==984
    store=module.ReaInputStore(factory)
    sources=store.freeze(plan,inputs,5000)['sources']
    attempts=[];responses=[];results=[]
    for request in plan['requests']:
        source=sources[request['coreBinding']['sourceKey']]
        response=response_for(request,source)
        payload=response.model_dump(mode='json')
        outcome=match_rea_inputs(request,source,response,5000)
        attempts.append({'request_key':request['requestKey'],'plan_fingerprint':plan['planFingerprint'],
            'run_id':'controlled','request':request,'page_size':5000,'state':'received','finished_at':datetime.now(UTC)})
        responses.append({'request_key':request['requestKey'],'payload':payload,'response_hash':_hash(payload)})
        results.append({'request_key':request['requestKey'],'core_binding':request['coreBinding'],
            'witness_version':module.WITNESS_VERSION,'status':outcome['status'],'outcome':outcome})
    with factory.begin() as session:
        session.execute(insert(models.ReaInputAttempt),attempts)
        session.execute(insert(models.ReaInputResponse),responses)
        session.execute(insert(models.ReaInputSupplement),results)
    original=module._decode_snapshot;calls=[]
    def decode(row):
        calls.append(row.fingerprint)
        return original(row)
    monkeypatch.setattr(module,'_decode_snapshot',decode)
    started=perf_counter()
    first=read_rea_progress(factory,plan,sources,5000)
    elapsed=perf_counter()-started
    assert first['processingCompleted'] and first['matchedCount']==984 and len(calls)==1
    with evidence_fixture_transaction(factory, models.ReaInputResponse) as session:
        session.get(models.ReaInputResponse,plan['requests'][-1]['requestKey']).response_hash='bad'
    second=read_rea_progress(factory,plan,sources,5000)
    assert second['blockedReasons']==['REA_EVIDENCE_INVALID'] and len(calls)==2
    print(f'Controlled 164 members / 984 durable targets: full progress {elapsed:.3f}s; snapshot decodes 1/pass; tail corruption blocked')
