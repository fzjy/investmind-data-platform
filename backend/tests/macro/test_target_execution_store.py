from backend.tests.macro.pg_evidence import evidence_fixture_transaction
from backend.tests.support.pg_snapshot import database_snapshot
"""执行状态使用真实PostgreSQL；所有请求/响应均为受控样例。"""
from copy import deepcopy
from datetime import date, datetime, UTC, timedelta
from importlib import import_module
from uuid import UUID

import pytest
from sqlalchemy import select, event

from app.db.session import create_engine_and_session
from app.connectors.tushare.contracts import TushareResponse
from app.stocks.universes.models import UniverseSelection
from backend.tests.macro.test_enterprise_target_catalog import payload, load

NOW=datetime(2026,10,22,8,tzinfo=UTC)
RUN=str(UUID(int=1)); OTHER=str(UUID(int=2))


def implementation():
    try: return import_module('app.macro.target_execution_store')
    except ImportError: pytest.fail('target execution store not implemented')


@pytest.fixture
def database(tmp_path, pg_factory):
    mod=implementation()
    models=import_module('app.db.models.enterprise_target_execution')
    factory = pg_factory
    engine = factory.kw['bind']
    value=payload(); value['evidencePolicy'].update(collectionEnabled=True,bootstrapTargetCodes=['000300.SH'])
    catalog=load(tmp_path,value)
    selection=UniverseSelection('custom','controlled','custom',('000001.SZ','600519.SH'),'b'*64)
    return mod.TargetExecutionStore(factory), models, engine, factory, catalog, selection


def root(database):
    store,_,_,_,catalog,selection=database
    return store.prepare(catalog,selection,date(2026,10,22),'Asia/Shanghai',persist=True)


def next_step(store,plan):
    return store.progress(plan,persist=False)['next']


def metadata_response():
    return TushareResponse(fields=('ts_code','name','fullname','market','publisher','exp_date'),
        items=(('000300.SH','沪深300','沪深300指数','CSI','中证',None),))


def receive_metadata(database,plan,run=RUN):
    store,*_=database
    step=next_step(store,plan); key=store.reserve(plan,step,run,NOW)
    store.receive(key,metadata_response(),NOW,database[4].evidence_policy)
    return key


def test_prepare_freezes_once_and_readonly_does_not_mutate_database(database, tmp_path):
    store,models,engine,factory,catalog,selection=database
    before=database_snapshot(engine); candidate=store.prepare(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    assert database_snapshot(engine)==before
    plan=root(database); old=database_snapshot(engine)
    assert store.prepare(catalog,selection,date(2026,10,25),'Asia/Shanghai')==plan==candidate
    assert database_snapshot(engine)==old
    value=payload(); value['catalogVersion']='changed'; altered=load(tmp_path,value)
    with pytest.raises(ValueError,match='TARGET_FROZEN_PLAN_CHANGED'):
        store.prepare(altered,selection,date(2026,10,25),'Asia/Shanghai',persist=True)
    with factory() as s: assert len(s.scalars(select(models.TargetExecutionPlan)).all())==1


def test_received_raw_replays_candidate_without_new_reservation(database):
    store,_,engine,factory,catalog,_=database; plan=root(database)
    receive_metadata(database,plan)
    from app.db.models.enterprise_target_evidence import TargetObservation,TargetCandidate
    with factory() as s:
        assert len(s.scalars(select(TargetObservation)).all())==1
        assert len(s.scalars(select(TargetCandidate)).all())==0
    before=database_snapshot(engine); preview=store.progress(plan,persist=False)
    assert preview['rootReservedCount']==1 and preview['replayPendingCount']==1
    assert database_snapshot(engine)==before
    done=store.progress(plan,persist=True)
    assert done['rootReservedCount']==1 and done['replayPendingCount']==0
    with factory() as s: assert len(s.scalars(select(TargetCandidate)).all())==1
    assert done['next']['request']['apiName']=='index_weight'


def test_run_and_root_budget_survive_new_engine_without_refunds(database):
    store,_,engine,factory,catalog,selection=database
    from dataclasses import replace
    catalog=replace(catalog,evidence_policy=replace(catalog.evidence_policy,max_http_per_run=1,max_http_per_plan=2))
    # 控制策略与catalogHash仍共同冻结，独立函数拒绝后续策略漂移。
    plan=store.prepare(catalog,selection,date(2026,10,22),'Asia/Shanghai',persist=True)
    key=store.reserve(plan,next_step(store,plan),RUN,NOW); store.receive(key,metadata_response(),NOW,catalog.evidence_policy)
    step=next_step(store,plan)
    with pytest.raises(ValueError,match='TARGET_RUN_BUDGET_EXHAUSTED'): store.reserve(plan,step,RUN,NOW)
    second,factory2=create_engine_and_session(engine.url.render_as_string(hide_password=False))
    try:
        resumed=implementation().TargetExecutionStore(factory2)
        key=resumed.reserve(plan,step,OTHER,NOW)
        resumed.receive(key,TushareResponse(fields=('index_code','con_code','trade_date','weight'),items=()),NOW,catalog.evidence_policy)
        with pytest.raises(ValueError,match='TARGET_ROOT_BUDGET_EXHAUSTED'):
            resumed.reserve(plan,next_step(resumed,plan),str(UUID(int=3)),NOW)
    finally: second.dispose()


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_or_failed_attempt_blocks_whole_root_without_resend(database,failed):
    store,_,_,_,catalog,_=database; plan=root(database)
    key=store.reserve(plan,next_step(store,plan),RUN,NOW)
    if failed: store.fail(key,'TARGET_NETWORK_ERROR',NOW)
    progress=store.progress(plan)
    assert progress['blockedReasons']==['TARGET_ATTEMPT_FAILED' if failed else 'TARGET_ATTEMPT_UNCERTAIN']
    assert progress['rootReservedCount']==1
    with pytest.raises(ValueError,match='TARGET_ATTEMPT_'): store.reserve(plan,progress['next'],OTHER,NOW)


def test_rate_limit_rejects_without_consuming_second_attempt(database):
    store,_,_,factory,catalog,selection=database
    from dataclasses import replace
    catalog=replace(catalog,evidence_policy=replace(catalog.evidence_policy,requests_per_minute=1))
    plan=store.prepare(catalog,selection,date(2026,10,22),'Asia/Shanghai',persist=True)
    key=store.reserve(plan,next_step(store,plan),RUN,NOW);store.receive(key,metadata_response(),NOW,catalog.evidence_policy)
    step=next_step(store,plan)
    with pytest.raises(ValueError,match='TARGET_RATE_LIMITED'):store.reserve(plan,step,OTHER,NOW+timedelta(seconds=59))
    assert store.progress(plan)['rootReservedCount']==1
    assert store.reserve(plan,step,OTHER,NOW+timedelta(seconds=60))


@pytest.mark.parametrize('damage',['request','raw','candidate'])
def test_any_existing_evidence_tampering_blocks_before_future_stage(database,damage):
    store,models,_,factory,_,_=database; plan=root(database); key=receive_metadata(database,plan)
    from app.db.models.enterprise_target_evidence import TargetObservation,TargetCandidate
    store.progress(plan,persist=True)
    affected = {"raw": (TargetObservation,), "candidate": (TargetCandidate,)}.get(damage, ())
    with evidence_fixture_transaction(factory, *affected) as s:
        if damage=='request':
            row=s.get(models.TargetExecutionAttempt,key); value=deepcopy(row.request);value['request']['params']['market']='wrong';row.request=value
        elif damage=='raw':
            row=s.scalars(select(TargetObservation)).one(); value=deepcopy(row.response);value['items'][0][0]='wrong';row.response=value
        else:
            row=s.scalars(select(TargetCandidate)).one();value=deepcopy(row.payload);value['metadata']['name']='changed';row.payload=value
    with pytest.raises(ValueError,match='TARGET_'):store.progress(plan,persist=True)


def test_response_and_received_state_commit_atomically(database):
    store,models,engine,factory,catalog,_=database;plan=root(database)
    key=store.reserve(plan,next_step(store,plan),RUN,NOW)
    def crash(_conn,_cursor,statement,_params,_context,_many):
        if statement.lstrip().upper().startswith('UPDATE') and 'enterprise_target_execution_attempt' in statement:
            raise RuntimeError('controlled crash')
    event.listen(engine,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError,match='controlled crash'):store.receive(key,metadata_response(),NOW,catalog.evidence_policy)
    finally:event.remove(engine,'before_cursor_execute',crash)
    from app.db.models.enterprise_target_evidence import TargetObservation
    with factory() as s:
        assert s.scalars(select(TargetObservation)).all()==[]
        assert s.get(models.TargetExecutionAttempt,key).state=='reserved'
