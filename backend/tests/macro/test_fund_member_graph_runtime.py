from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
import pytest

import httpx
from sqlalchemy import select

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_fund_member_graph import NOW,value
from backend.tests.macro.test_fund_member_graph_store import setup as metadata,csi_bytes
from backend.tests.macro.test_official_pcf import body as pcf_body
from backend.tests.macro.test_enterprise_target_catalog import load


def setup(factory,tmp_path,monkeypatch,one=False,limit=4):
    v=value();v['catalogEvidence'].update(collectionEnabled=True,memberGraphEnabled=True)
    v['officialEvidence']['maxHttpPerRun']=limit
    if one:v['catalogEvidence']['memberGraphs']=v['catalogEvidence']['memberGraphs'][:1]
    c,_=metadata(factory,tmp_path,v=v)
    if not one:metadata(factory,tmp_path,'515220.SH',v=v)
    m=import_module('app.macro.jobs.fund_member_graph_runtime');monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    return m,c


def handler(factory,calls,fail_first=False,bad_csi=False):
    active=['512660']
    def handle(request):
        calls.append(str(request.url));is_csi=request.url.path.endswith('cons.xls')
        if is_csi:active[0]='515220' if '399998' in request.url.path else '512660'
        kind='csi-members' if is_csi else 'fund-pcf'
        with factory() as session:
            row=session.scalar(select(JobRun).where(JobRun.business_key=='fund-member-graph:'+active[0]+'.SH:'+kind))
            assert row is not None and row.request_count==1
        if fail_first and len(calls)==1:return response(403)
        if is_csi:return response(200,content=b'invalid but retained' if bad_csi else csi_bytes('399998' if active[0]=='515220' else '399967'))
        assert request.url.path.endswith('/20261022')
        return response(200,content=pcf_body(code=active[0],day='20261022'))
    return handle


def test_two_graphs_share_daily_limit_and_finish_across_controlled_days_without_statement_refetch(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(f,tmp_path,monkeypatch);calls=[];transport=httpx.MockTransport(handler(f,calls))
    first=m.execute_fund_graphs(f,c,clock=lambda:NOW,transport=transport)
    assert first['supplierHttp']==2 and [g['status'] for g in first['groups']]==['completed','budget_pending']
    assert count(f,JobRun)==4 and count(f,SourcePayload)==4
    second=m.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)
    assert second['supplierHttp']==2 and second['configuredGraphsCompleted'] and count(f,JobRun)==6
    again=m.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)
    assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==4


def test_zero_daily_balance_never_creates_new_graph_root(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(f,tmp_path,monkeypatch)
    from backend.tests.macro.test_official_daily_budget_switch import fill_day
    fill_day(f,tmp_path,total=2)
    out=m.execute_fund_graphs(f,c,clock=lambda:NOW)
    assert out['readOnly'] and out['supplierHttp']==0 and all(g['status']=='budget_pending' for g in out['groups'])
    assert count(f,JobRun)==4


def test_failed_scope_is_frozen_but_other_graph_can_use_remaining_slot(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(f,tmp_path,monkeypatch);calls=[]
    result=m.execute_fund_graphs(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handler(f,calls,fail_first=True)))
    assert result['supplierHttp']==2 and [g['status'] for g in result['groups']]==['blocked','budget_pending']


def test_invalid_saved_csi_replays_only_zero_http(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(f,tmp_path,monkeypatch,one=True);calls=[];transport=httpx.MockTransport(handler(f,calls,bad_csi=True))
    result=m.execute_fund_graphs(f,c,clock=lambda:NOW,transport=transport)
    assert result['supplierHttp']==1 and result['groups'][0]['status']=='replay_pending'
    again=m.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)
    assert again['supplierHttp']==0 and len(calls)==1 and count(f,SourcePayload)==2


def test_disabled_and_live_configuration_change_send_nothing(database,tmp_path,monkeypatch):
    m=import_module('app.macro.jobs.fund_member_graph_runtime');c=load(tmp_path,value())
    monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    assert m.run_fund_graphs(SimpleNamespace())['status']=='disabled'
    _,f=database;m,c=setup(f,tmp_path,monkeypatch,one=True)
    changed=load(tmp_path,value())
    out=m.execute_fund_graphs(f,c,clock=lambda:NOW,before_send=lambda:monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:changed))
    assert out['supplierHttp']==0 and count(f,JobRun)==1


def test_budget_receipt_is_refreshed_when_other_attempts_take_last_slots(database,tmp_path,monkeypatch):
    _,f=database;m,c=setup(f,tmp_path,monkeypatch,one=True)
    def fill():
        from backend.tests.macro.test_official_daily_budget_switch import fill_day
        fill_day(f,tmp_path,total=3)
    out=m.execute_fund_graphs(f,c,clock=lambda:NOW,before_send=fill)
    assert out['supplierHttp']==0 and out['groups'][0]['status']=='budget_pending'
    assert out['groups'][0]['dailyBudget']['used']==4 and out['groups'][0]['dailyBudget']['remaining']==0


@pytest.mark.parametrize('phase,status,public',[('failed','blocked','source-failed'),('reserved','blocked','request-pending'),('received','replay_pending','replay-pending')])
def test_parent_contract_failure_unknown_and_replay_are_not_flattened(database,tmp_path,monkeypatch,phase,status,public):
    from app.macro.fund_contract_store import FundContractStore
    from app.macro.fund_contract_discovery import build_discovery_plan
    from app.macro.enterprise_official_targets import load_official_source_states
    from backend.tests.macro.test_fund_contract_discovery import body
    _,f=database;v=value();v['catalogEvidence'].update(collectionEnabled=True,memberGraphEnabled=True)
    c=load(tmp_path,v);parent=FundContractStore(f);p=build_discovery_plan(c,'fund-512660',NOW);key=parent.reserve(p,NOW)
    if phase=='failed':parent.fail(key,'EXTENSION_AUTH_ERROR',NOW)
    elif phase=='received':parent.receive(key,body(),NOW)
    m=import_module('app.macro.jobs.fund_member_graph_runtime');monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    out=m.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(days=1))
    assert out['supplierHttp']==0 and out['groups'][0]['status']==status
    assert load_official_source_states(f,NOW+timedelta(days=1),c)['512660.SH']['status']==public
    if phase=='failed':assert out['groups'][0]['errorCode']=='EXTENSION_AUTH_ERROR'
