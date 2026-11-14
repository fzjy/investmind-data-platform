"""每日上限是运行时选项；无限额仍计数、验账且不重置冻结根。"""
from importlib import import_module

import httpx
import pytest

from app.db.models.ops import JobRun
from app.macro.official_member_budget import daily_budget
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import NOW, value
from backend.tests.macro.test_enterprise_target_catalog import load


def catalog(tmp_path, enabled=True):
    v=value();v['officialEvidence']['dailyBudgetEnabled']=enabled
    v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True)
    return load(tmp_path,v)


def fill_day(factory,tmp_path,total=4):
    from backend.tests.macro.test_catalog_member_policy import value,source
    from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
    v=value();codes=[f'{910+i:06d}' for i in range(total)]
    for code in codes:
        v['targets'].append({'kind':'index','code':code+'.SH','label':'离线测试指数','sourceIndexNameHint':None})
        v['catalogEvidence']['targets'].append({'code':code+'.SH','documents':[source(code)]})
    c=load(tmp_path,v);s=CatalogMemberStore(factory,daily_budget_enabled=False);keys=[]
    for code in codes:
        p=build_catalog_member_plan(c,code+'.SH',NOW)
        keys.append(s.reserve(p,p['policy']['documents'][0],NOW))
    return keys


def test_runtime_switch_is_strict_and_preserves_frozen_source_identity(tmp_path):
    from app.macro.issuer_contract_samples import build_sample_plan
    limited=catalog(tmp_path);unlimited=catalog(tmp_path,False)
    assert unlimited.official_policy.dailyBudgetEnabled is False
    assert limited.fingerprint==unlimited.fingerprint
    assert limited.official_policy.fingerprint==unlimited.official_policy.fingerprint
    assert build_sample_plan(limited,'512800',NOW)==build_sample_plan(unlimited,'512800',NOW)
    for bad in (0,1,'false',None):
        v=value();v['officialEvidence']['dailyBudgetEnabled']=bad
        with pytest.raises(ValueError):load(tmp_path,v)


def test_unlimited_budget_still_counts_and_rejects_invalid_reservations(database,tmp_path):
    _,f=database;keys=fill_day(f,tmp_path)
    with f() as session:
        state=daily_budget(session,NOW,None)
        assert state['limit'] is None and state['remaining'] is None and state['used']==4
        assert daily_budget(session,NOW,4)['remaining']==0
    with f.begin() as session:session.get(JobRun,keys[0]).request_count=0
    with f() as session:
        with pytest.raises(ValueError,match='BUDGET_INVALID'):daily_budget(session,NOW,None)


def test_unlimited_samples_send_after_four_and_reentry_does_not_resend(database,tmp_path,monkeypatch):
    _,f=database;fill_day(f,tmp_path);m=import_module('app.macro.jobs.issuer_sample_runtime')
    c=catalog(tmp_path,False);monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):calls.append(str(request.url));return response(200,content=b'opaque issuer original')
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==2 and len(calls)==2
    assert out['groups'][-1]['dailyBudget']['used']==6
    assert out['groups'][-1]['dailyBudget']['remaining'] is None
    again=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert again['supplierHttp']==0 and len(calls)==2 and count(f,JobRun)==6


def test_switch_changed_during_execution_stops_send(database,tmp_path,monkeypatch):
    _,f=database;m=import_module('app.macro.jobs.issuer_sample_runtime');c=catalog(tmp_path,False)
    current=[c];monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:current[0])
    def before_send():current[0]=catalog(tmp_path,True)
    calls=[]
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,before_send=before_send,
        transport=httpx.MockTransport(lambda r:calls.append(r) or response(200,content=b'bad')))
    assert out['supplierHttp']==0 and not calls and count(f,JobRun)==0
    assert out['groups'][0]['errorCode']=='OFFICIAL_MEMBER_COLLECTION_CHANGED'


@pytest.mark.parametrize('kind',['official','catalog','discovery','graph'])
def test_every_store_can_reserve_after_daily_limit_without_changing_plan(database,tmp_path,kind):
    _,f=database
    if kind=='graph':
        from backend.tests.macro.test_fund_member_graph_store import setup
        _,p=setup(f,tmp_path)
        cls=import_module('app.macro.fund_member_graph_store').FundMemberGraphStore
        args=(p,'csi-members',NOW)
    elif kind=='official':
        from backend.tests.macro.test_official_member_store import plan
        p=plan(NOW);cls=import_module('app.macro.official_member_store').OfficialMemberStore
        args=(p,p['policy']['documents'][0],NOW)
    elif kind=='catalog':
        from backend.tests.macro.test_catalog_member_policy import plan
        p=plan(tmp_path);cls=import_module('app.macro.catalog_member_store').CatalogMemberStore
        args=(p,p['policy']['documents'][0],NOW)
    else:
        from backend.tests.macro.test_fund_contract_discovery import plan
        p=plan(tmp_path);cls=import_module('app.macro.fund_contract_store').FundContractStore
        args=(p,NOW)
    fill_day(f,tmp_path)
    with pytest.raises(ValueError,match='DAILY_BUDGET'):cls(f).reserve(*args)
    s=cls(f,daily_budget_enabled=False);s.reserve(*args)
    state=s.inspect(p,NOW)
    assert state['dailyBudget']['limit'] is None
    assert state.get('plan',state.get('intent'))==p
    with pytest.raises(ValueError):s.reserve(*args)


def test_changing_switch_keeps_failed_sample_frozen(database,tmp_path,monkeypatch):
    _,f=database;m=import_module('app.macro.jobs.issuer_sample_runtime');v=value()
    v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True)
    v['catalogEvidence']['sampleDocuments']=v['catalogEvidence']['sampleDocuments'][:1]
    v['officialEvidence']['dailyBudgetEnabled']=False;c=load(tmp_path,v)
    current=[c];monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:current[0]);calls=[]
    def handle(request):calls.append(str(request.url));return response(403)
    first=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert first['supplierHttp']==1 and len(calls)==1
    v['officialEvidence']['dailyBudgetEnabled']=True;current[0]=load(tmp_path,v)
    again=m.execute_issuer_samples(f,current[0],clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert again['supplierHttp']==0 and len(calls)==1 and count(f,JobRun)==1
    assert again['groups'][0]['status']=='blocked'


def test_unlimited_daily_total_still_obeys_per_run_limit(database,tmp_path,monkeypatch):
    _,f=database;fill_day(f,tmp_path);m=import_module('app.macro.jobs.issuer_sample_runtime');v=value()
    v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True)
    v['officialEvidence'].update(dailyBudgetEnabled=False,maxHttpPerRun=1)
    c=load(tmp_path,v);monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c);calls=[]
    def handle(request):calls.append(str(request.url));return response(200,content=b'opaque original')
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert out['supplierHttp']==1 and len(calls)==1
    assert out['groups'][1]['status']=='budget_pending'
    again=m.execute_issuer_samples(f,c,clock=lambda:NOW,transport=httpx.MockTransport(handle))
    assert again['supplierHttp']==1 and len(calls)==2 and count(f,JobRun)==6
