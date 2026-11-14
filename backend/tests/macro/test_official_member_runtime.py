from datetime import timedelta
from importlib import import_module
import json

import httpx
import pytest

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_enterprise_target_catalog import payload
from backend.tests.macro.test_official_member_policy import policy
from backend.tests.macro.test_official_member_store import NOW
from backend.tests.macro.official_xls_fixture import compound_workbook


def setup(tmp_path,monkeypatch,two=False):
    import app.macro.enterprise_target_catalog as module
    value=payload();p=policy();p['collectionEnabled']=True
    if two:
        value['targets'][1]={'kind':'index','code':'000905.SH','label':'中证500','sourceIndexNameHint':None}
        value['evidencePolicy']['bootstrapTargetCodes']=['000300.SH','000905.SH']
        p['bootstrapTargetCodes']=['000300.SH','000905.SH'];p['maxHttpPerRun']=1;p['maxHttpPerDay']=2
        p['documents']=p['documents'][:2]
        p['documents'][1].update(id='csi-000905',indexCode='000905',url=p['documents'][1]['url'].replace('399975','000905'))
    else:p['bootstrapTargetCodes']=['000300.SH'];p['documents']=p['documents'][:1]
    value['officialEvidence']=p
    file=tmp_path/'official-targets.json';file.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
    monkeypatch.setattr(module,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    return file,load_enterprise_target_catalog()


def execute(factory,catalog,**kwargs):
    return import_module('app.macro.jobs.official_member_runtime').execute_official_members(factory,catalog,clock=lambda:NOW,**kwargs)


def test_transport_observes_durable_reservation_and_reentry_zero(database,tmp_path,monkeypatch):
    _,factory=database;_,c=setup(tmp_path,monkeypatch);calls=[]
    def handler(request):
        assert count(factory,JobRun)==1 and count(factory,SourcePayload)==0
        calls.append(request);return response(200,content=compound_workbook())
    first=execute(factory,c,transport=httpx.MockTransport(handler))
    assert first['status']=='completed' and first['supplierHttp']==first['responseCount']==1
    assert not first['readOnly'] and count(factory,SourcePayload)==1
    again=execute(factory,c,transport=httpx.MockTransport(handler))
    assert again['status']=='completed' and again['supplierHttp']==0 and again['readOnly'] and len(calls)==1


def test_request_budget_resumes_one_plus_one_then_zero(database,tmp_path,monkeypatch):
    _,factory=database;_,c=setup(tmp_path,monkeypatch,two=True);calls=[]
    def handler(request):
        calls.append(request);code='000905' if '000905' in str(request.url) else '000300'
        from backend.tests.macro.test_official_members import row
        return response(200,content=compound_workbook([row(index=code,indexName='测试指数')]))
    a=execute(factory,c,transport=httpx.MockTransport(handler));b=execute(factory,c,transport=httpx.MockTransport(handler));z=execute(factory,c,transport=httpx.MockTransport(handler))
    assert (a['status'],a['supplierHttp'])==('partial',1)
    assert (b['status'],b['supplierHttp'])==('completed',1)
    assert z['supplierHttp']==0 and len(calls)==2 and count(factory,JobRun)==2


def test_http_rejection_stops_and_next_day_cannot_refund(database,tmp_path,monkeypatch):
    _,factory=database;_,c=setup(tmp_path,monkeypatch);calls=[]
    def handler(request):calls.append(request);return response(403,content=b'secret response not logged')
    first=execute(factory,c,transport=httpx.MockTransport(handler))
    assert first['status']=='blocked' and first['supplierHttp']==1 and first['errorCode']=='EXTENSION_AUTH_ERROR'
    later=import_module('app.macro.jobs.official_member_runtime').execute_official_members(factory,c,clock=lambda:NOW+timedelta(days=1),transport=httpx.MockTransport(handler))
    assert later['supplierHttp']==0 and later['readOnly'] and len(calls)==1


def test_bad_document_keeps_raw_and_reentry_only_replays(database,tmp_path,monkeypatch):
    _,factory=database;_,c=setup(tmp_path,monkeypatch);calls=[]
    def handler(request):calls.append(request);return response(200,content=b'not valid xls')
    first=execute(factory,c,transport=httpx.MockTransport(handler));again=execute(factory,c,transport=httpx.MockTransport(handler))
    assert first['status']==again['status']=='replay_pending'
    assert first['supplierHttp']==1 and again['supplierHttp']==0 and len(calls)==1
    assert count(factory,SourcePayload)==1 and count(factory,JobRun)==1


def test_collection_switch_rechecked_after_rate_wait_before_reservation(database,tmp_path,monkeypatch):
    _,factory=database;file,c=setup(tmp_path,monkeypatch)
    def disable():
        value=json.loads(file.read_text());value['officialEvidence']['collectionEnabled']=False
        file.write_text(json.dumps(value),encoding='utf8')
    report=execute(factory,c,before_send=disable,transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert report['status']=='blocked' and report['supplierHttp']==0 and report['readOnly']
    assert count(factory,JobRun)==0
