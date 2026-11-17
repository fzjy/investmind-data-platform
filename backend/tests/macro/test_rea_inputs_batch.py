from backend.tests.macro.pg_evidence import evidence_fixture_transaction
from dataclasses import replace
from datetime import date
import importlib
import importlib.util
import json

import httpx
import pytest
from sqlalchemy import select

from app.macro.enterprise import EnterprisePolicy
from app.db.session import create_engine_and_session
from backend.tests.macro.test_rea_input_store import database, frozen, response_for
from backend.tests.macro.test_rea_input_preview import data

AS_OF=date(2024,8,1)
POLICY=EnterprisePolicy(min_samples=1)


def implementation():
    assert importlib.util.find_spec('app.macro.jobs.rea_inputs_batch'), 'REA budgeted batch missing'
    return importlib.import_module('app.macro.jobs.rea_inputs_batch')


def execute(factory,inputs=None,*,limit=1,calls=None,probe=False,source=None,change=None,as_of=AS_OF,page_size=5000):
    inputs=inputs or data(1)
    calls=calls if calls is not None else []
    def handle(request):
        payload=json.loads(request.content)
        calls.append(payload)
        table='balance' if payload['api_name']=='balancesheet' else 'cashflow'
        original=next(r for r in inputs.financials if r.table==table and r.symbol==payload['params']['ts_code']
            and r.end_date.strftime('%Y%m%d')==payload['params']['period'])
        from app.macro.rea_input_preview import rea_input_request
        response=response_for(rea_input_request(original,inputs.universe_version),original,change)
        return httpx.Response(200,json={'code':0,'data':response.model_dump(mode='json')})
    return implementation().execute_rea_inputs_batch(factory,inputs,as_of,POLICY,quarters=1,request_limit=limit,
        page_size=page_size,token='controlled-token',transport=httpx.MockTransport(handle),probe=probe,source=source)


def test_total_budget_and_new_engine_resume_same_target_zero_http(database):
    _,_,url,engine,factory=database
    calls=[]
    first=execute(factory,calls=calls)
    assert first['requestCount']==1 and first['pendingCount']==5
    engine.dispose();again,new_factory=create_engine_and_session(url)
    try:
        for _ in range(5): assert execute(new_factory,calls=calls)['requestCount']==1
        final=execute(new_factory,calls=calls)
        assert final['status']=='completed' and final['processingCompleted']
        assert final['requestCount']==0 and len(calls)==6
        assert {c['api_name'] for c in calls}=={'balancesheet','cashflow'}
    finally: again.dispose()


def test_source_probe_is_one_fresh_attempt_and_cache_is_not_permission_proof(database):
    _,_,_,_,factory=database
    calls=[]
    first=execute(factory,limit=100,calls=calls,probe=True,source='cashflow')
    assert first['requestCount']==1 and first['httpLimit']==1 and first['probeReady']
    assert calls[0]['api_name']=='cashflow'
    for _ in range(3): execute(factory,limit=100,calls=calls,probe=True,source='cashflow')
    cached=execute(factory,limit=100,calls=calls,probe=True,source='cashflow')
    assert cached['requestCount']==0 and not cached['probeReady'] and len(calls)==4


def test_partial_fields_are_terminal_gaps_and_not_retried(database):
    _,_,_,_,factory=database
    calls=[]
    result=execute(factory,limit=100,calls=calls,change={'fix_assets':None})
    assert result['status']=='completed-with-gaps' and result['processingCompleted']
    assert result['fieldUnavailableReasons']['returned-null']==2
    assert execute(factory,limit=100,calls=calls)['requestCount']==0 and len(calls)==6


def test_raw_without_core_is_replayed_even_if_http_budget_is_zero_remaining(database):
    store,plan,inputs,request,original=frozen(database)
    _,models,_,_,factory=database
    store.reserve(request,plan['planFingerprint'],'crash',5000)
    store.receive(request['requestKey'],response_for(request,original))
    pending=implementation().read_rea_progress(factory,plan,store.latest()['sources'],5000)
    assert not pending['processingCompleted'] and pending['replayPendingCount']==1
    calls=[]
    result=execute(factory,inputs,limit=100,calls=calls)
    assert result['processingCompleted'] and result['requestCount']==5 and len(calls)==5
    with factory() as session: assert len(list(session.scalars(select(models.ReaInputSupplement))))==6


def test_crash_after_committed_raw_resumes_on_new_engine_without_resending(database,monkeypatch):
    from app.macro.rea_input_store import ReaInputStore
    _,_,url,engine,factory=database
    original_receive=ReaInputStore.receive
    calls=[]
    def crash_after_raw(self,*args):
        original_receive(self,*args)
        raise RuntimeError('controlled interruption after Raw commit')
    with monkeypatch.context() as patch:
        patch.setattr(ReaInputStore,'receive',crash_after_raw)
        with pytest.raises(RuntimeError,match='controlled interruption'):
            execute(factory,limit=100,calls=calls)
    assert len(calls)==1
    engine.dispose();again,new_factory=create_engine_and_session(url)
    try:
        resumed=execute(new_factory,limit=100,calls=calls)
        assert resumed['processingCompleted'] and resumed['requestCount']==5 and len(calls)==6
        assert execute(new_factory,limit=100,calls=calls)['requestCount']==0
    finally: again.dispose()


@pytest.mark.parametrize('failure',['network','server','permission'])
def test_transport_failure_uses_one_reservation_and_never_automatically_retries(database,failure):
    _,_,_,_,factory=database
    calls=[]
    def handle(request):
        calls.append(request)
        if failure=='network':
            raise httpx.ConnectError('controlled private transport error',request=request)
        if failure=='server':
            return httpx.Response(503)
        return httpx.Response(200,json={'code':-1,'msg':'controlled permission denied'})
    result=implementation().execute_rea_inputs_batch(factory,data(1),AS_OF,POLICY,
        quarters=1,request_limit=100,page_size=5000,token='controlled-token',transport=httpx.MockTransport(handle))
    assert result['status']=='blocked' and result['requestCount']==1 and result['responseCount']==0
    assert len(calls)==1
    assert not any(secret in json.dumps(result) for secret in
        ('controlled-token','controlled private transport error','controlled permission denied'))
    retry_calls=[]
    again=execute(factory,limit=100,calls=retry_calls)
    assert again['status']=='blocked' and again['requestCount']==0 and retry_calls==[]
    assert again['errorCode']=='REA_ATTEMPT_FAILED'


@pytest.mark.parametrize('failure',['uncertain','failed','later-corrupt'])
def test_all_stored_evidence_is_checked_before_any_new_http(database,failure):
    store,plan,inputs,_,_=frozen(database)
    request=plan['requests'][-1]
    original=next(r for r in inputs.financials if r.source_key==request['coreBinding']['sourceKey'])
    _,models,_,_,factory=database
    store.reserve(request,plan['planFingerprint'],'crash',5000)
    if failure=='failed': store.fail(request['requestKey'],'TushareNetworkError')
    elif failure=='later-corrupt':
        store.receive(request['requestKey'],response_for(request,original))
        with evidence_fixture_transaction(factory, models.ReaInputResponse) as session: session.get(models.ReaInputResponse,request['requestKey']).response_hash='0'*64
    calls=[]
    result=execute(factory,inputs,limit=100,calls=calls)
    assert result['status']=='blocked' and result['requestCount']==0 and calls==[]


@pytest.mark.parametrize('change',['source','pool','budget','page'])
def test_pending_frozen_plan_does_not_silently_change_scope(database,change):
    _,_,_,_,factory=database
    original=data(1);execute(factory,original,limit=1)
    changed=original
    if change=='source': changed=replace(original,financials=tuple(replace(r,values={**r.values,'money_cap':999}) if r.table=='balance' else r for r in original.financials))
    elif change=='pool': changed=replace(original,universe_version='changed')
    calls=[]
    result=execute(factory,changed,limit=2 if change=='budget' else 1,page_size=4000 if change=='page' else 5000,calls=calls)
    assert result['status']=='blocked' and result['requestCount']==0 and calls==[]


def test_pending_plan_retains_original_quarter_across_realistic_date_advance(database):
    _,_,_,_,factory=database
    execute(factory,limit=1)
    calls=[]
    resumed=execute(factory,limit=1,as_of=date(2024,11,1),calls=calls)
    assert resumed['requestCount']==1 and resumed['outputPeriods']==['2024-06-30']


def changed_inputs(value):
    original=data(1)
    return replace(original,financials=tuple(replace(r,values={**r.values,'money_cap':value})
        if r.table=='balance' else r for r in original.financials))


def test_completed_historical_plan_can_be_reused_when_inputs_return_on_a_later_day(database):
    from datetime import UTC, datetime
    _,models,_,_,factory=database
    first=execute(factory,data(1),limit=100,as_of=date(2024,8,1))
    second=execute(factory,changed_inputs(123),limit=100,as_of=date(2024,8,2))
    assert first['processingCompleted'] and second['processingCompleted']
    with evidence_fixture_transaction(factory, models.ReaInputPlan) as session:
        session.get(models.ReaInputPlan,first['planFingerprint']).created_at=datetime(2024,8,1,tzinfo=UTC)
        session.get(models.ReaInputPlan,second['planFingerprint']).created_at=datetime(2024,8,2,tzinfo=UTC)
    calls=[]
    returned=execute(factory,data(1),limit=100,as_of=date(2024,8,3),calls=calls)
    assert returned['status']=='completed' and returned['processingCompleted'] and returned['requestCount']==0
    assert returned['frozenAsOfDate']=='2024-08-01' and calls==[]


def test_tied_timestamps_cannot_hide_new_pending_plan_or_bypass_its_freeze(database):
    from datetime import UTC, datetime
    from app.macro.rea_input_store import ReaInputStore
    from backend.tests.macro.test_rea_input_preview import preview
    _,models,_,_,factory=database
    first=execute(factory,limit=100)
    for value in range(100,200):
        inputs=changed_inputs(value)
        plan=preview(inputs,limit=100)
        if plan['planFingerprint']<first['planFingerprint']:
            break
    else: pytest.fail('controlled candidate with reverse fingerprint ordering missing')
    store=ReaInputStore(factory)
    store.freeze(plan,inputs,5000)
    with evidence_fixture_transaction(factory, models.ReaInputPlan) as session:
        for row in session.scalars(select(models.ReaInputPlan)):
            row.created_at=datetime(2024,8,1,tzinfo=UTC)
    assert store.latest()['plan']['planFingerprint']==plan['planFingerprint']
    calls=[]
    candidate=execute(factory,changed_inputs(999),limit=100,calls=calls)
    assert candidate['status']=='blocked' and candidate['errorCode']=='REA_FROZEN_PLAN_CHANGED' and calls==[]


def test_progress_decodes_plan_once_per_validation_pass_and_still_checks_tail_evidence(database,monkeypatch):
    from app.macro import rea_input_store as module
    store,plan,inputs,_,_=frozen(database)
    _,models,_,_,factory=database
    for request in plan['requests']:
        source=next(r for r in inputs.financials if r.source_key==request['coreBinding']['sourceKey'])
        store.reserve(request,plan['planFingerprint'],'controlled',5000)
        store.receive(request['requestKey'],response_for(request,source))
        store.recover(request,source,5000,persist=True)
    calls=[];original=module._decode_snapshot
    def count(row):
        calls.append(row.fingerprint)
        return original(row)
    monkeypatch.setattr(module,'_decode_snapshot',count)
    result=implementation().read_rea_progress(factory,plan,store.latest()['sources'],5000)
    # One for latest(), one for this independent full progress pass, not per target.
    assert result['processingCompleted'] and len(calls)==2
    with evidence_fixture_transaction(factory, models.ReaInputResponse) as session:
        session.get(models.ReaInputResponse,plan['requests'][-1]['requestKey']).response_hash='bad'
    again=implementation().read_rea_progress(factory,plan,store.latest()['sources'],5000)
    assert again['blockedReasons']==['REA_EVIDENCE_INVALID'] and not again['processingCompleted']
