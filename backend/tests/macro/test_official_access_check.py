"""官方403父级单次诊断：隔离SQLite/假HTTP，不连接供应商。"""
from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import httpx
import pytest
from sqlalchemy import select

from app.db.models import IngestionBatch,JobCursor,JobRun,MacroObservation,SourcePayload
from app.macro.extensions.config import ExtensionConfig,load_extension_config
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_official_macro_store import plan,store
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_household_credit import document

CHECK=NOW+timedelta(seconds=10)


def module():
    try:return import_module('app.macro.official_access_check')
    except ModuleNotFoundError:pytest.fail('official access diagnostic not implemented')


def prepared(factory):
    p=plan();s=store(factory);key=s.reserve(p,p['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    config=load_extension_config().model_dump();value=policy();value['collectionEnabled']=True
    config['groups']['householdCredit']=value
    return key,ExtensionConfig.model_validate(config)


def state(factory):
    result={}
    with factory() as s:
        for model in (JobRun,JobCursor,IngestionBatch,SourcePayload,MacroObservation):
            result[model.__tablename__]=[deepcopy({c.name:getattr(row,c.name) for c in model.__table__.columns})
                for row in s.scalars(select(model).order_by(model.id))]
    return result


def execute(factory,parent,config,**kwargs):
    return module().execute_official_access_check(factory,parent,config,clock=kwargs.pop('clock',lambda:CHECK),
        config_loader=kwargs.pop('config_loader',lambda:config),**kwargs)


def test_preview_is_readonly_and_unknown_id_is_not_queried_as_new_attempt(database):
    _,f=database;parent,c=prepared(f);before=state(f)
    result=execute(f,parent,c,dry_run=True,transport=httpx.MockTransport(lambda _:pytest.fail('no network')))
    assert result['status']=='preview' and result['readOnly'] and result['supplierHttp']==0
    assert result['additionalHttpBudget']==1 and state(f)==before
    with pytest.raises(ValueError):module().OfficialAccessCheckStore(f,'not-a-uuid')


@pytest.mark.parametrize('status',[403,302,429,500])
def test_durable_one_attempt_failure_and_zero_reentry_after_new_period(database,status):
    _,f=database;parent,c=prepared(f);before=state(f);calls=[]
    def handler(request):
        calls.append(request)
        assert count(f,JobRun)==2 and count(f,IngestionBatch)==2
        assert str(request.url)==c.groups['householdCredit'].documents[0].url
        assert not any(k.lower() in ('authorization','cookie') for k in request.headers)
        return response(status,content=b'not saved on HTTP failure')
    first=execute(f,parent,c,transport=httpx.MockTransport(handler))
    second=execute(f,parent,c,clock=lambda:CHECK+timedelta(days=40),transport=httpx.MockTransport(handler))
    assert first['supplierHttp']==1 and second['supplierHttp']==0 and len(calls)==1
    assert first['phase']==second['phase']=='failed'
    assert first['status']==second['status']=='blocked' and count(f,SourcePayload)==count(f,MacroObservation)==0
    after=state(f)
    assert next(r for r in after['job_run'] if r['id']==parent)==before['job_run'][0] and after['job_cursor']==before['job_cursor']
    assert store(f).inspect(plan(),CHECK)['status']=='blocked'


@pytest.mark.parametrize('body,valid',[(document(title='2026年前八个月金融统计数据报告'),True),
    (document(),False),(b'',False),(b'<title>unknown</title>',False)])
def test_success_raw_is_preserved_but_never_written_to_core_or_declared_recovered(database,body,valid):
    _,f=database;parent,c=prepared(f);calls=[]
    def handler(request):calls.append(request);return response(200,content=body)
    result=execute(f,parent,c,transport=httpx.MockTransport(handler))
    again=execute(f,parent,c,transport=httpx.MockTransport(handler))
    assert result['status']==('response_valid' if valid else 'response_invalid')
    assert again['status']==result['status'] and result['supplierHttp']==1 and again['supplierHttp']==0
    assert result['recoveryCompleted'] is False and result['insertedRows']==0 and count(f,MacroObservation)==0
    assert count(f,SourcePayload)==1 and store(f).inspect(plan(),CHECK)['status']=='blocked'


def test_unconfirmed_reserved_slot_cannot_be_resent(database):
    _,f=database;parent,c=prepared(f);m=module();s=m.OfficialAccessCheckStore(f,parent)
    s.reserve(c,CHECK)
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('reserved is uncertain')))
    assert result['status']=='blocked' and result['supplierHttp']==0 and result['errorCode']=='OFFICIAL_ACCESS_ATTEMPT_UNCERTAIN'


def test_received_only_replays_original_and_parent_or_raw_tamper_blocks(database):
    _,f=database;parent,c=prepared(f);m=module();s=m.OfficialAccessCheckStore(f,parent)
    key=s.reserve(c,CHECK);s.receive(c,document(title='2026年前八个月金融统计数据报告'),CHECK)
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('received must not send')))
    assert result['status']=='response_valid' and result['supplierHttp']==0
    with f.begin() as session:session.scalar(select(SourcePayload)).content_hash='bad'
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('tamper must not send')))
    assert result['status']=='blocked' and result['supplierHttp']==0


@pytest.mark.parametrize('kind',['parent','config','parent-type'])
def test_changed_context_or_non_auth_failure_is_rejected_without_io(database,kind):
    _,f=database;parent,c=prepared(f)
    if kind=='parent':
        with f.begin() as session:session.get(JobRun,parent).request_count=2
    elif kind=='parent-type':
        with f.begin() as session:session.get(JobRun,parent).error_message='EXTENSION_TIMEOUT'
    else:
        value=c.model_dump();value['groups']['householdCredit']['lookbackPeriods']=47;c=ExtensionConfig.model_validate(value)
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('invalid context must not send')))
    assert result['status']=='blocked' and result['supplierHttp']==0 and count(f,JobRun)==1


def test_switch_is_rechecked_before_reserving_or_sending(database):
    _,f=database;parent,c=prepared(f);value=c.model_dump();value['groups']['householdCredit']['collectionEnabled']=False
    off=ExtensionConfig.model_validate(value)
    result=execute(f,parent,c,config_loader=lambda:off,transport=httpx.MockTransport(lambda _:pytest.fail('disabled')))
    assert result['supplierHttp']==0 and count(f,JobRun)==1


def test_transport_timeout_is_not_retried_and_error_does_not_echo_body_or_secret(database):
    _,f=database;parent,c=prepared(f);calls=[]
    def handler(request):calls.append(request);raise httpx.ConnectTimeout('private diagnostic text')
    result=execute(f,parent,c,transport=httpx.MockTransport(handler))
    assert result['supplierHttp']==1 and len(calls)==1 and result['errorCode']=='EXTENSION_TIMEOUT'
    assert 'private diagnostic' not in str(result)


def test_diagnostic_timestamp_cannot_precede_its_original_failure(database):
    _,f=database;parent,c=prepared(f);m=module();s=m.OfficialAccessCheckStore(f,parent)
    key=s.reserve(c,CHECK)
    with f.begin() as session:
        session.get(JobRun,key).started_at=NOW-timedelta(days=1)
        session.get(IngestionBatch,key).started_at=NOW-timedelta(days=1)
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('invalid chronology')))
    assert result['supplierHttp']==0 and result['errorCode']=='OFFICIAL_ACCESS_CONTEXT_INVALID'


def test_uncertain_reservation_commit_does_not_send_or_reissue(database,monkeypatch):
    _,f=database;parent,c=prepared(f);m=module();original=m.OfficialAccessCheckStore.reserve
    def uncertain(self,*a,**kw):original(self,*a,**kw);raise RuntimeError('private commit detail')
    monkeypatch.setattr(m.OfficialAccessCheckStore,'reserve',uncertain)
    first=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('uncertain commit')))
    again=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('reserved must not resend')))
    assert first['supplierHttp']==again['supplierHttp']==0 and first['readOnly'] is False
    assert first['phase']=='unknown' and again['phase']=='reserved' and count(f,JobRun)==2
    assert 'private commit detail' not in str(first)


@pytest.mark.parametrize('stage',['receive','finish'])
def test_postcommit_fault_reenters_without_http_and_preserves_original_parent(database,monkeypatch,stage):
    _,f=database;parent,c=prepared(f);m=module();original=getattr(m.OfficialAccessCheckStore,stage);calls=[]
    def uncertain(self,*a,**kw):original(self,*a,**kw);raise RuntimeError('private after-commit detail')
    monkeypatch.setattr(m.OfficialAccessCheckStore,stage,uncertain)
    def handler(request):calls.append(request);return response(200,content=document(title='2026年前八个月金融统计数据报告'))
    first=execute(f,parent,c,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(m.OfficialAccessCheckStore,stage,original)
    again=execute(f,parent,c,transport=httpx.MockTransport(handler))
    assert first['supplierHttp']==1 and again['supplierHttp']==0 and len(calls)==1 and first['readOnly'] is False
    assert first['phase']=='unknown'
    assert again['status']=='response_valid' and count(f,SourcePayload)==1 and count(f,MacroObservation)==0
    assert store(f).inspect(plan(),CHECK)['status']=='blocked'


def test_parent_change_while_waiting_is_rejected_before_new_request(database):
    _,f=database;parent,c=prepared(f)
    def change():
        with f.begin() as s:
            s.get(JobRun,parent).finished_at=NOW+timedelta(seconds=2)
            s.get(IngestionBatch,parent).finished_at=NOW+timedelta(seconds=2)
    result=execute(f,parent,c,before_send=change,transport=httpx.MockTransport(lambda _:response(200,content=b'')))
    assert result['supplierHttp']==0 and count(f,JobRun)==1


def test_final_read_failure_after_completion_reports_unknown_not_the_previous_phase(database,monkeypatch):
    _,f=database;parent,c=prepared(f);m=module();s=m.OfficialAccessCheckStore(f,parent)
    s.reserve(c,CHECK);s.receive(c,document(title='2026年前八个月金融统计数据报告'),CHECK)
    original=m.OfficialAccessCheckStore.inspect
    def uncertain(self,*a,**kw):
        result=original(self,*a,**kw)
        if result['phase']=='completed':raise RuntimeError('private read failure')
        return result
    monkeypatch.setattr(m.OfficialAccessCheckStore,'inspect',uncertain)
    result=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('received never sends')))
    assert result['phase']=='unknown' and result['readOnly'] is False and result['supplierHttp']==0
    monkeypatch.setattr(m.OfficialAccessCheckStore,'inspect',original)
    again=execute(f,parent,c,transport=httpx.MockTransport(lambda _:pytest.fail('completed never sends')))
    assert again['status']=='response_valid' and again['supplierHttp']==0
