from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import httpx
import pytest

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_fund_contract_discovery import NOW,value,body
from backend.tests.macro.test_enterprise_target_catalog import load


def setup(tmp_path,monkeypatch,limit=4):
    m=import_module('app.macro.jobs.fund_contract_runtime');v=value()
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True);v['officialEvidence']['maxHttpPerRun']=limit
    c=load(tmp_path,v);monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    return m,c


def test_normal_entry_reserves_before_http_and_completed_next_day_is_zero_http(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(tmp_path,monkeypatch);calls=[]
    def handle(request):
        code=request.url.path.split('/')[-1]
        with f() as session:
            from sqlalchemy import select
            assert session.scalar(select(JobRun).where(JobRun.business_key=='fund-contract:'+code)).request_count==1
        calls.append(code)
        return response(200,content=body(code,'中证军工指数' if code=='512660' else '中证煤炭指数',resource='123456'))
    transport=httpx.MockTransport(handle)
    out=m.execute_fund_contracts(f,c,clock=lambda:NOW,transport=transport)
    assert out['status']=='completed' and out['supplierHttp']==2 and not out['proofReady'],[(g['documentId'],g.get('errorCode')) for g in out['groups']]
    assert all(g['normalized']['downloadResourceKey']=='123456' for g in out['groups'])
    assert count(f,SourcePayload)==2 and count(f,JobRun)==2
    again=m.execute_fund_contracts(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)
    assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==2


def test_failure_and_invalid_saved_html_are_isolated_and_not_retried(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(tmp_path,monkeypatch);calls=[]
    def handle(request):
        calls.append(request.url.path)
        return response(403) if len(calls)==1 else response(200,content=b'<html>saved invalid</html>')
    out=m.execute_fund_contracts(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['status']=='blocked' and out['supplierHttp']==2
    assert [g['status'] for g in out['groups']]==['blocked','replay_pending'],[(g['documentId'],g.get('errorCode')) for g in out['groups']]
    again=m.execute_fund_contracts(f,c,clock=lambda:NOW+timedelta(days=1),transport=httpx.MockTransport(handle))
    assert again['supplierHttp']==0 and len(calls)==2 and count(f,SourcePayload)==1


def test_shared_run_limit_and_config_drift_before_reservation(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(tmp_path,monkeypatch,1);calls=[]
    def handle(request):
        calls.append(request.url.path);return response(200,content=body())
    out=m.execute_fund_contracts(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==1 and out['groups'][1]['status']=='budget_pending'
    assert count(f,JobRun)==1


def test_disabled_entry_never_opens_database_and_live_configuration_change_blocks(database,tmp_path,monkeypatch):
    m=import_module('app.macro.jobs.fund_contract_runtime');v=value();c=load(tmp_path,v)
    monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    assert m.run_fund_contracts(SimpleNamespace())['status']=='disabled'
    _,f=database;m,c=setup(tmp_path,monkeypatch)
    v=value();v['catalogEvidence']['collectionEnabled']=True
    changed=load(tmp_path,v)
    out=m.execute_fund_contracts(f,c,clock=lambda:NOW,before_send=lambda:monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:changed))
    assert out['supplierHttp']==0 and count(f,JobRun)==0


def test_ambiguous_post_commit_reservation_keeps_slot_but_sends_nothing(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(tmp_path,monkeypatch,1)
    cls=import_module('app.macro.fund_contract_store').FundContractStore;reserve=cls.reserve
    def ambiguous(self,p,at):reserve(self,p,at);raise RuntimeError('commit acknowledgement unavailable')
    monkeypatch.setattr(cls,'reserve',ambiguous)
    out=m.execute_fund_contracts(f,c,clock=lambda:NOW)
    assert out['supplierHttp']==0 and out['requestCount']==1 and count(f,JobRun)==1
    assert out['groups'][0]['errorCode']=='FUND_CONTRACT_DISCOVERY_RESERVATION_UNCERTAIN'
