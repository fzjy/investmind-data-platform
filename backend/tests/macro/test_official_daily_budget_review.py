"""审查反例：预留后开关变化、损坏旧根、最终计数。"""
from dataclasses import replace
from datetime import timedelta
from importlib import import_module

import httpx
import pytest

from app.db.models.ops import JobRun
from app.macro.official_member_budget import daily_budget
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_catalog_member_policy import NOW


def setup(kind,factory,tmp_path,monkeypatch):
    if kind=='official':
        from backend.tests.macro.test_official_member_runtime import setup
        _,c=setup(tmp_path,monkeypatch)
        return import_module('app.macro.jobs.official_member_runtime'),c,'OfficialMemberStore','execute_official_members'
    if kind=='catalog':
        from backend.tests.macro.test_catalog_member_runtime import setup
        c,m=setup(tmp_path,monkeypatch)
        return m,c,'CatalogMemberStore','execute_catalog_members'
    if kind=='discovery':
        from backend.tests.macro.test_fund_contract_runtime import setup
        m,c=setup(tmp_path,monkeypatch)
        return m,c,'FundContractStore','execute_fund_contracts'
    from backend.tests.macro.test_fund_member_graph_runtime import setup
    m,c=setup(factory,tmp_path,monkeypatch,one=True)
    return m,c,'FundMemberGraphStore','execute_fund_graphs'


@pytest.mark.parametrize('kind',['official','catalog','discovery','graph'])
def test_switch_changes_after_reservation_block_the_actual_transport(database,tmp_path,monkeypatch,kind):
    _,f=database;m,c,store_name,execute=setup(kind,f,tmp_path,monkeypatch)
    c=replace(c,official_policy=c.official_policy.model_copy(update={'dailyBudgetEnabled':False}))
    current=[c];monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:current[0])
    cls=getattr(m,store_name);original=cls.reserve;calls=[]
    def reserve(self,*args,**kwargs):
        key=original(self,*args,**kwargs)
        current[0]=replace(c,official_policy=c.official_policy.model_copy(update={'dailyBudgetEnabled':True}))
        return key
    monkeypatch.setattr(cls,'reserve',reserve)
    out=getattr(m,execute)(f,c,clock=lambda:NOW,
        transport=httpx.MockTransport(lambda r:calls.append(r) or response(200,content=b'unreachable')))
    assert out['requestCount']==1
    assert out['supplierHttp']==0 and not calls
    errors=[out.get('errorCode')]+[g.get('errorCode') for g in out.get('groups',[])]
    assert 'OFFICIAL_MEMBER_COLLECTION_CHANGED' in errors


@pytest.mark.parametrize('job',['enterprise-official-members-v1','enterprise-official-catalog-v1:000905.SH'])
def test_daily_counter_rejects_broken_old_roots_even_when_not_counted_today(database,job):
    _,f=database
    with f.begin() as session:
        session.add(JobRun(id='broken-history',job_name=job,business_key='broken',status='running',
            started_at=NOW-timedelta(days=1),request_count=1,row_count=0,details={}))
    with f() as session:
        with pytest.raises(ValueError,match='BUDGET_INVALID'):daily_budget(session,NOW,None)


@pytest.mark.parametrize('kind',['official','catalog','discovery'])
def test_execution_reports_the_post_reservation_budget(database,tmp_path,monkeypatch,kind):
    _,f=database;m,c,_,execute=setup(kind,f,tmp_path,monkeypatch)
    if kind=='catalog':
        from backend.tests.macro.test_catalog_member_runtime import handler
        transport=httpx.MockTransport(handler(f,[]))
    elif kind=='official':
        from backend.tests.macro.official_xls_fixture import compound_workbook
        transport=httpx.MockTransport(lambda r:response(200,content=compound_workbook()))
    else:
        from backend.tests.macro.test_fund_contract_discovery import body
        def handle(request):
            code=request.url.path.split('/')[-1]
            target=next(t for t in c.targets if t.code[:6]==code)
            return response(200,content=body(code,target.source_index_name_hint,resource='512661' if code=='512660' else '515221'))
        transport=httpx.MockTransport(handle)
    out=getattr(m,execute)(f,c,clock=lambda:NOW,transport=transport)
    assert out['supplierHttp']>0
    if kind=='official':assert out['dailyBudget']['used']==out['requestCount']
    else:
        budgets=[g['dailyBudget'] for g in out['groups'] if 'dailyBudget' in g]
        assert budgets[0]['used']==1
        assert budgets[-1]['used']==out['requestCount']
