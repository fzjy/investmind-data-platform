from datetime import timedelta
from importlib import import_module

import httpx
import pytest

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.macro.extensions.config import ExtensionConfig, load_extension_config
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_property_loans import directory, REPORT, report


def setup(monkeypatch,group='householdCredit',cap=None):
    value=load_extension_config().model_dump();p=policy(group);p['collectionEnabled']=True
    if cap:p['limits']['maxHttpPerRun']=cap
    value['groups'][group]=p
    config=ExtensionConfig.model_validate(value)
    module=import_module('app.macro.jobs.official_macro_runtime')
    monkeypatch.setattr(module,'load_extension_config',lambda:config)
    return module,config


def test_transport_observes_durable_slot_and_same_target_reentry_zero(database,monkeypatch):
    _,factory=database;m,c=setup(monkeypatch);calls=[]
    def handler(request):
        calls.append(request)
        assert count(factory,JobRun)==len(calls)
        body=document(title='2026年前八个月金融统计数据报告') if len(calls)==1 else document()
        return response(200,content=body)
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    again=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert first['status']=='completed' and first['supplierHttp']==first['responseCount']==2
    assert again['status']=='completed' and again['readOnly'] and again['supplierHttp']==0 and len(calls)==2


def test_single_run_cap_resumes_one_plus_one_then_zero(database,monkeypatch):
    _,factory=database;m,c=setup(monkeypatch,cap=1);calls=[]
    def handler(request):
        calls.append(request)
        return response(200,content=document(title='2026年前八个月金融统计数据报告') if len(calls)==1 else document())
    values=[m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(3)]
    assert [(v['status'],v['supplierHttp']) for v in values]==[('partial',1),('completed',1),('completed',0)]


@pytest.mark.parametrize('status,code',[(403,'EXTENSION_AUTH_ERROR'),(302,'EXTENSION_REDIRECT_REFUSED'),(429,'EXTENSION_RATE_LIMITED'),(500,'EXTENSION_SERVER_ERROR')])
def test_http_failure_stops_no_raw_and_next_week_does_not_refund(database,monkeypatch,status,code):
    _,factory=database;m,c=setup(monkeypatch);calls=[]
    def handler(request):calls.append(request);return response(status,content=b'do not log body')
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    later=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW+timedelta(days=8),transport=httpx.MockTransport(handler))
    assert first['supplierHttp']==1 and first['errorCode']==code and first['status']=='blocked'
    assert later['supplierHttp']==0 and later['readOnly'] and len(calls)==1
    assert count(factory,SourcePayload)==0


def test_bad_document_saved_and_reentry_only_zero_http_replay(database,monkeypatch):
    _,factory=database;m,c=setup(monkeypatch);calls=[]
    def handler(request):calls.append(request);return response(200,content=b'<h1>unknown</h1>')
    reports=[m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(2)]
    assert [r['status'] for r in reports]==['replay_pending','replay_pending']
    assert [r['supplierHttp'] for r in reports]==[1,0] and len(calls)==1 and count(factory,SourcePayload)==1


def test_switch_rechecked_after_wait_and_no_request_slot_written(database,monkeypatch):
    _,factory=database;m,c=setup(monkeypatch)
    def disable():
        value=c.model_dump();value['groups']['householdCredit']['collectionEnabled']=False
        monkeypatch.setattr(m,'load_extension_config',lambda:ExtensionConfig.model_validate(value))
    result=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,before_send=disable,
        transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert result['supplierHttp']==0 and result['readOnly'] and result['status']=='blocked'
    assert count(factory,JobRun)==0


@pytest.mark.parametrize('found',[False,True])
def test_property_only_requests_a_link_present_in_saved_directory(database,monkeypatch,found):
    _,factory=database;m,c=setup(monkeypatch,'propertyLoans');urls=[]
    def handler(request):
        urls.append(str(request.url))
        if len(urls)==1:return response(200,content=directory([(REPORT,'2026年二季度金融机构贷款投向统计报告')] if found else []))
        assert str(request.url)==REPORT
        return response(200,content=report())
    result=m.execute_official_macro_group(factory,c,'propertyLoans',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert result['status']==('completed' if found else 'source_pending')
    assert result['supplierHttp']==(2 if found else 1)
    again=m.execute_official_macro_group(factory,c,'propertyLoans',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert again['supplierHttp']==0 and again['readOnly']
