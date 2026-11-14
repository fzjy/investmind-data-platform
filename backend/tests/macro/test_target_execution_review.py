"""独立审查反例：单根全表、字面量精确CHECK、未完成步骤可达性。"""
from copy import deepcopy

import pytest
from sqlalchemy import CheckConstraint, MetaData, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.schema import BLANK_SCHEMA

from app.macro.target_evidence_contract import evidence_hash
from app.macro.target_evidence_schema import _sql
from app.macro.target_execution_plan import execution_step, step_request
from app.macro.target_execution_schema import target_execution_schema
from backend.tests.macro.test_target_execution_store import database, root, NOW, RUN  # noqa: F401


def test_second_slot_is_rejected_by_database_constraint(database):
    _,models,_,_,factory,_,_=database
    with pytest.raises(IntegrityError),factory.begin() as s:
        s.add(models.TargetExecutionPlan(slot='second',fingerprint='a'*64,snapshot={},snapshot_hash='a'*64))


def test_rogue_plan_row_blocks_prepare_and_progress_even_if_check_was_bypassed(database):
    store,models,_,engine,factory,catalog,selection=database;plan=root(database)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        c.execute(models.TargetExecutionPlan.__table__.insert().values(slot='second',fingerprint='a'*64,snapshot={},snapshot_hash='a'*64))
        c.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    for call in (lambda:store.progress(plan),lambda:store.prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True)):
        with pytest.raises(ValueError,match='TARGET_EXECUTION_INVALID'):call()


@pytest.mark.parametrize('literal',["re served","RESERVED","res(erved)"])
def test_different_literal_schema_is_rejected_before_any_plan_write(database,literal):
    store,models,_,engine,factory,catalog,selection=database
    from app.db.base import TargetEvidenceBase
    metadata=MetaData()
    for table in TargetEvidenceBase.metadata.sorted_tables:
        table.to_metadata(metadata,schema=None,referred_schema_fn=lambda *args:BLANK_SCHEMA)
    attempt=metadata.tables['enterprise_target_execution_attempt']
    for constraint in attempt.constraints:
        if isinstance(constraint,CheckConstraint):
            constraint.sqltext=text(f"state = '{literal}' OR state = 'received' OR state = 'failed'")
    attempt.drop(engine);attempt.create(engine)
    assert 'ops.enterprise_target_execution_attempt:check' in target_execution_schema(factory)
    with pytest.raises(ValueError,match='TARGET_EXECUTION_SCHEMA_REQUIRED'):
        store.prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True)
    with factory() as s:assert s.scalars(select(models.TargetExecutionPlan)).all()==[]


@pytest.mark.parametrize('actual',[
    "((state)::text = 'reserved'::text) OR ((state)::text = 'received'::text) OR ((state)::text = 'failed'::text)",
    "state = 'reserved' OR (state = 'received' OR state = 'failed')",
])
def test_only_known_sql_formatting_and_associative_or_are_equivalent(actual):
    assert _sql(actual)==_sql("state = 'reserved' OR state = 'received' OR state = 'failed'")


@pytest.mark.parametrize('actual',[
    "state = 'reserved' AND state = 'received' OR state = 'failed'",
    "state = 'reserved' OR state = 'received' AND state = 'failed'",
    "state = 'reserved' OR state = 'received' OR state = 'failed' OR true",
    "state = 'reserved'::character varying(2) OR state = 'received' OR state = 'failed'",
])
def test_unknown_or_semantically_different_expressions_fail_closed(actual):
    assert _sql(actual)!=_sql("state = 'reserved' OR state = 'received' OR state = 'failed'")


@pytest.mark.parametrize('state',['reserved','failed'])
def test_pending_request_must_be_current_graph_frontier(database,state):
    store,models,_,_,factory,_,_=database;plan=root(database)
    step=deepcopy(store.progress(plan)['next'])
    step['request'].update(apiName='index_weight',params={'index_code':'000300.SH','start_date':'20261001','end_date':'20261022'},
        fields=['index_code','con_code','trade_date','weight'])
    step=execution_step(plan,step_request(step),[])
    with factory.begin() as s:
        s.add(models.TargetExecutionAttempt(request_key=step['requestKey'],plan_fingerprint=plan['fingerprint'],
            run_id=RUN,request=step,request_hash=evidence_hash(step),state=state,created_at=NOW,
            **({'finished_at':NOW,'error_code':'TARGET_NETWORK_ERROR'} if state=='failed' else {})))
    with pytest.raises(ValueError,match='TARGET_EXECUTION_INVALID'):store.progress(plan,persist=True)
