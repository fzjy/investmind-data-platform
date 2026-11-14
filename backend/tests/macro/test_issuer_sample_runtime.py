from copy import deepcopy
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.db.models.ops import JobRun,JobCursor
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_issuer_contract_samples import NOW,value


def setup(tmp_path,monkeypatch,limit=4):
    m=import_module('app.macro.jobs.issuer_sample_runtime');v=value()
    v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True);v['officialEvidence']['maxHttpPerRun']=limit
    c=load(tmp_path,v);monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    return m,c,v


def test_reserve_commit_precedes_get_and_saved_samples_are_zero_http_next_day(database,tmp_path,monkeypatch):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch);calls=[]
    def handle(request):
        with f() as session:assert len(session.scalars(select(JobRun)).all())==len(calls)+1
        calls.append(str(request.url));return response(200,content=b'opaque original: not verified')
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==2 and out['requestCount']==2 and not out['proofReady']
    assert out['status']=='samples_received' and all(g['status']=='sample_received' for g in out['groups'])
    assert all(g['sample']['identityVerified'] is False for g in out['groups'])
    assert out['groups'][-1]['dailyBudget']['used']==2
    again=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(days=1),transport=httpx.MockTransport(handle))
    assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==2
    assert count(f,SourcePayload)==2 and count(f,JobCursor)==2


@pytest.mark.parametrize('status',[302,403,429,500])
def test_failed_requests_freeze_without_retry_or_redirect(database,tmp_path,monkeypatch,status):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch);calls=[]
    def handle(request):calls.append(str(request.url));return response(status,headers={'location':'https://example.org'})
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==2 and out['status']=='blocked' and count(f,SourcePayload)==0
    again=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(days=1),transport=httpx.MockTransport(handle))
    assert again['supplierHttp']==0 and len(calls)==2


def test_limit_zero_budget_disabled_and_changed_policy_do_not_send(database,tmp_path,monkeypatch):
    _,f=database;m,c,v=setup(tmp_path,monkeypatch,1)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(lambda r:response(200,content=b'x')))
    assert out['supplierHttp']==1 and out['groups'][1]['status']=='budget_pending'
    v['catalogEvidence']['sampleEnabled']=False;closed=load(tmp_path,v)
    monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:closed)
    assert m.run_issuer_samples(SimpleNamespace())['status']=='disabled'
    changed=m.execute_issuer_samples(f,c,clock=lambda:NOW,before_send=lambda:None)
    assert changed['supplierHttp']==0 and count(f,JobRun)==1


def test_unfinished_member_graphs_take_precedence_over_samples(database,tmp_path,monkeypatch):
    from backend.tests.macro.test_fund_member_graph import value as graph_value
    _,f=database;m,_,v=setup(tmp_path,monkeypatch)
    graph=graph_value()
    for target in graph['targets']:
        if target['code'] not in {t['code'] for t in v['targets']}:v['targets'].append(target)
    for target in graph['catalogEvidence']['targets']:
        if target['code'] not in {t['code'] for t in v['catalogEvidence']['targets']}:v['catalogEvidence']['targets'].append(target)
    for key in ('discoveryDocuments','memberGraphs'):v['catalogEvidence'][key]=deepcopy(graph['catalogEvidence'][key])
    c=load(tmp_path,v);monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW)
    assert out['supplierHttp']==0 and out['readOnly'] and count(f,JobRun)==0
    assert all(g['status']=='member_graph_pending' for g in out['groups'])
    assert out['memberGraphPrerequisites'] and not any(g['witnessReady'] for g in out['memberGraphPrerequisites'])


def test_reservation_acknowledgement_loss_never_sends_or_refunds(database,tmp_path,monkeypatch):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch,1)
    cls=import_module('app.macro.issuer_sample_store').IssuerSampleStore;reserve=cls.reserve
    def lost(self,p,at):reserve(self,p,at);raise RuntimeError('commit acknowledgement unavailable')
    monkeypatch.setattr(cls,'reserve',lost)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW)
    assert out['supplierHttp']==0 and out['requestCount']==1 and count(f,JobRun)==1
    assert out['groups'][0]['status']=='blocked'


def test_body_size_and_empty_are_frozen_failures(database,tmp_path,monkeypatch):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch);calls=[]
    def handle(request):
        calls.append(str(request.url))
        return response(200,content=b'') if len(calls)==1 else response(200,content=b'x',headers={'content-length':'8388609'})
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['status']=='blocked' and out['supplierHttp']==2 and count(f,SourcePayload)==0
    assert count(f,JobRun)==2


def test_all_four_committed_attempts_leave_zero_new_roots(database,tmp_path,monkeypatch):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch)
    from backend.tests.macro.test_official_daily_budget_switch import fill_day
    fill_day(f,tmp_path);before=count(f,JobCursor)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW)
    assert out['supplierHttp']==0 and out['readOnly'] and count(f,JobCursor)==before
    assert all(g['status']=='budget_pending' and g['dailyBudget']['remaining']==0 for g in out['groups'])


def test_actual_completed_three_receipt_graph_releases_only_remaining_slot(database,tmp_path,monkeypatch):
    from backend.tests.macro.test_fund_member_graph import value as graph_value
    from backend.tests.macro.test_fund_member_graph_store import setup as seed,store as graph_store,complete_csi
    from backend.tests.macro.test_official_pcf import body as pcf
    _,f=database;m,_,v=setup(tmp_path,monkeypatch);graph=graph_value()
    for target in graph['targets']:
        if target['code'] not in {t['code'] for t in v['targets']}:v['targets'].append(target)
    for target in graph['catalogEvidence']['targets']:
        if target['code'] not in {t['code'] for t in v['catalogEvidence']['targets']}:v['catalogEvidence']['targets'].append(target)
    v['catalogEvidence']['discoveryDocuments']=graph['catalogEvidence']['discoveryDocuments']
    v['catalogEvidence']['memberGraphs']=graph['catalogEvidence']['memberGraphs'][:1]
    c,p=seed(f,tmp_path,v=v);s=graph_store(f);complete_csi(s,p)
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(key,pcf(code='512660',day='20261022'),NOW+timedelta(seconds=5))
    s.replay(key,NOW+timedelta(seconds=6));monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(seconds=7),transport=httpx.MockTransport(lambda r:response(200,content=b'opaque')))
    assert out['memberGraphPrerequisites'][0]['witnessReady'] and out['supplierHttp']==1
    assert [g['status'] for g in out['groups']]==['sample_received','budget_pending']


def test_cli_contract_and_no_date_or_force_options(monkeypatch):
    import json
    from typer.testing import CliRunner
    from app.cli.main import app
    m=import_module('app.macro.jobs.issuer_sample_runtime');calls=[]
    def run(settings,**kwargs):calls.append(kwargs);return {'status':'blocked','supplierHttp':0,'readOnly':True,'proofReady':False}
    monkeypatch.setattr(m,'run_issuer_samples',run)
    result=CliRunner().invoke(app,['capture-enterprise-issuer-samples','--dry-run'])
    assert result.exit_code==2 and json.loads(result.stdout)['supplierHttp']==0 and calls==[{'dry_run':True}]
    for arg in ('--date','--force','--url'):
        assert CliRunner().invoke(app,['capture-enterprise-issuer-samples',arg]).exit_code==2
    assert len(calls)==1


def test_uncertain_reservation_stops_later_targets_even_with_run_capacity(database,tmp_path,monkeypatch):
    _,f=database;m,c,_=setup(tmp_path,monkeypatch,4);calls=[]
    cls=import_module('app.macro.issuer_sample_store').IssuerSampleStore;reserve=cls.reserve;reserved=[]
    def lost(self,p,at):
        key=reserve(self,p,at);reserved.append(key)
        if len(reserved)==1:raise RuntimeError('commit acknowledgement unavailable')
        return key
    monkeypatch.setattr(cls,'reserve',lost)
    def handle(request):calls.append(str(request.url));return response(200,content=b'opaque')
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==0 and out['requestCount']==1 and calls==[] and count(f,JobRun)==1
    assert out['groups'][1]['errorCode']=='ISSUER_SAMPLE_RUN_HALTED'


@pytest.mark.parametrize('change',['disabled','parent-invalid'])
def test_post_reservation_guard_retains_slot_without_http(database,tmp_path,monkeypatch,change):
    from backend.tests.macro.test_fund_member_graph import value as graph_value
    from backend.tests.macro.test_fund_member_graph_store import setup as seed,store as graph_store,complete_csi
    from backend.tests.macro.test_official_pcf import body as pcf
    from app.db.models.raw import IngestionBatch
    _,f=database;m,_,v=setup(tmp_path,monkeypatch);graph=graph_value()
    for target in graph['targets']:
        if target['code'] not in {t['code'] for t in v['targets']}:v['targets'].append(target)
    for target in graph['catalogEvidence']['targets']:
        if target['code'] not in {t['code'] for t in v['catalogEvidence']['targets']}:v['catalogEvidence']['targets'].append(target)
    v['catalogEvidence']['discoveryDocuments']=graph['catalogEvidence']['discoveryDocuments']
    v['catalogEvidence']['memberGraphs']=graph['catalogEvidence']['memberGraphs'][:1]
    c,p=seed(f,tmp_path,v=v);s=graph_store(f);complete_csi(s,p)
    parent=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(parent,pcf(code='512660',day='20261022'),NOW+timedelta(seconds=5))
    s.replay(parent,NOW+timedelta(seconds=6));monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    cls=import_module('app.macro.issuer_sample_store').IssuerSampleStore;reserve=cls.reserve
    def changed(self,p,at):
        key=reserve(self,p,at)
        if change=='disabled':
            v['catalogEvidence']['sampleEnabled']=False;closed=load(tmp_path,v)
            monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:closed)
        else:
            with f.begin() as session:session.get(IngestionBatch,parent).status='failed'
        return key
    monkeypatch.setattr(cls,'reserve',changed);calls=[]
    def handle(request):calls.append(str(request.url));return response(200,content=b'opaque')
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(seconds=7),transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==0 and out['requestCount']==1 and calls==[] and count(f,JobRun)==4
    assert out['groups'][0]['status']=='blocked'
    if change=='parent-invalid':assert not out['memberGraphPrerequisites'][0]['witnessReady']
