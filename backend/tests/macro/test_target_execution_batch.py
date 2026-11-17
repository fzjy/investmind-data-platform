from backend.tests.support.pg_snapshot import database_snapshot
"""真实仓库＋受控HTTP测物理上限/跨次恢复；无真实供应商。"""
from datetime import timedelta
from importlib import import_module
import json

import httpx
import pytest
from sqlalchemy import select

from backend.tests.macro.test_target_execution_store import database,NOW,RUN,metadata_response  # noqa: F401
from backend.tests.macro.test_enterprise_target_catalog import payload,load


def module():
    try:return import_module('app.macro.jobs.target_execution_batch')
    except ImportError:pytest.fail('target execution batch not implemented')


def provider(calls,*,error=False):
    def send(request):
        value=json.loads(request.content);calls.append((value['api_name'],value['params']))
        if error:return httpx.Response(503,stream=httpx.ByteStream(b'unavailable'))
        fields=value['fields'].split(',');params=value['params']
        if value['api_name']=='etf_basic':items=[['512880.SH','证券ETF','399975.SZ','证券','SH','L','境内']]
        elif value['api_name']=='index_basic':items=[[params['ts_code'],'指数','完整指数名','CSI','中证',None]]
        else:items=[[params['index_code'],'600519.SH','20261020',0]]
        return httpx.Response(200,stream=httpx.ByteStream(json.dumps({'code':0,'data':{'fields':fields,'items':items}}).encode()))
    return httpx.MockTransport(send)


def test_complete_then_next_day_reentry_is_zero_http_even_without_token(database):
    _,models,_,factory,catalog,selection=database;calls=[]
    result=module().execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert result['requestCount']==result['responseCount']==2
    assert result['processingCompleted'] is True and result['membershipReady'] is False
    assert [name for name,_ in calls]==['index_basic','index_weight']
    again=module().execute_target_batch(factory,catalog,selection,(NOW+timedelta(days=1)).date(),'Asia/Shanghai','',transport=provider(calls),now=NOW+timedelta(days=1))
    assert again['requestCount']==0 and again['rootReservedCount']==2 and len(calls)==2
    assert again['frozenAsOfDate']=='2026-10-22'
    with factory() as s:assert len(s.scalars(select(models.TargetExecutionPlan)).all())==1


def test_etf_dynamic_dependencies_resume_two_requests_per_run(database, tmp_path):
    _,_,engine,factory,_,selection=database
    value=payload();value['evidencePolicy']['collectionEnabled']=True;catalog=load(tmp_path,value);calls=[]
    from app.db.session import create_engine_and_session
    mod=module()
    first=mod.execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert first['requestCount']==2 and not first['processingCompleted']
    engine.dispose(); fresh,factory2=create_engine_and_session(engine.url.render_as_string(hide_password=False))
    try:
        second=mod.execute_target_batch(factory2,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
        third=mod.execute_target_batch(factory2,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
        assert [first['requestCount'],second['requestCount'],third['requestCount']]==[2,2,1]
        assert third['processingCompleted'] and third['rootReservedCount']==5
        assert calls[3]==('index_basic',{'ts_code':'399975.SZ','market':'CSI'})
        assert calls[4][1]['index_code']=='399975.SZ'
    finally:fresh.dispose()


def test_failure_reservation_is_not_refunded_or_retried(database):
    _,_,_,factory,catalog,selection=database;calls=[];mod=module()
    first=mod.execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls,error=True),now=NOW)
    assert first['requestCount']==1 and first['responseCount']==0 and len(calls)==1
    second=mod.execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert second['requestCount']==0 and second['rootReservedCount']==1 and len(calls)==1
    assert second['errorCode']=='TARGET_ATTEMPT_FAILED'


def test_disabled_collection_does_not_even_prepare_or_open_transport(database):
    _,models,engine,factory,catalog,selection=database
    from dataclasses import replace
    catalog=replace(catalog,evidence_policy=replace(catalog.evidence_policy,collection_enabled=False))
    before=database_snapshot(engine);calls=[]
    result=module().execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert result['status']=='disabled' and result['requestCount']==0 and result['readOnly']
    assert database_snapshot(engine)==before and calls==[]


def test_uncertain_attempt_is_preserved_and_missing_candidate_replayed_without_resend(database):
    store,_,_,factory,catalog,selection=database
    root=store.prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True)
    key=store.reserve(root,store.progress(root)['next'],RUN,NOW);calls=[];mod=module()
    result=mod.execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert result['errorCode']=='TARGET_ATTEMPT_UNCERTAIN' and calls==[]
    store.receive(key,metadata_response(),NOW,catalog.evidence_policy)
    result=mod.execute_target_batch(factory,catalog,selection,NOW.date(),'Asia/Shanghai','token',transport=provider(calls),now=NOW)
    assert result['rootReservedCount']==2 and result['requestCount']==1
    assert result['processingCompleted'] and [api for api,_ in calls]==['index_weight']
