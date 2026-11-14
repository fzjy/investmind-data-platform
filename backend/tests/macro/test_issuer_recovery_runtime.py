from dataclasses import replace
from datetime import timedelta
import gzip
from importlib import import_module

import httpx
import pytest

from app.db.models.ops import JobRun,JobCursor
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_sample_recovery import seed,NOW


@pytest.mark.parametrize('code',['512400','512200','512980'])
def test_recovery_uses_registered_transport_then_zero_http_on_reentry(database,tmp_path,monkeypatch,code):
    _,f=database;c,parent=seed(f,tmp_path,code);m=import_module('app.macro.jobs.issuer_sample_runtime');calls=[]
    monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    def handle(request):
        assert count(f,JobRun)==2
        calls.append(str(request.url))
        return response(200,content=gzip.compress(b'<html>recovered original</html>'),headers={'Content-Encoding':'gzip'})
    transport=httpx.MockTransport(handle);at=NOW+timedelta(seconds=2)
    out=m.execute_issuer_samples(f,c,clock=lambda:at,transport=transport,recovery=True)
    assert out['supplierHttp']==1 and out['status']=='samples_received' and out['proofReady'] is False
    assert calls==['https://www.gffunds.com.cn/proxy/pcflist/512980' if code=='512980' else f'https://www.nffund.com/new/personal-financing/detail.html?fundCode={code}']
    again=m.execute_issuer_samples(f,c,clock=lambda:at+timedelta(days=1),transport=transport,recovery=True)
    assert again['supplierHttp']==0 and len(calls)==1
    with f() as session:assert session.get(JobRun,parent).status=='failed'


def test_switch_changed_after_recovery_reservation_sends_no_http(database,tmp_path,monkeypatch):
    _,f=database;c,_=seed(f,tmp_path);m=import_module('app.macro.jobs.issuer_sample_runtime')
    from app.macro.issuer_recovery_store import IssuerRecoveryStore
    current=[c];monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:current[0]);original=IssuerRecoveryStore.reserve
    def reserve(self,*args):
        key=original(self,*args);current[0]=replace(c,official_policy=c.official_policy.model_copy(update={'dailyBudgetEnabled':True}));return key
    monkeypatch.setattr(IssuerRecoveryStore,'reserve',reserve);calls=[]
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(seconds=2),recovery=True,
        transport=httpx.MockTransport(lambda r:calls.append(r) or response(200,content=b'impossible')))
    assert out['supplierHttp']==0 and out['requestCount']==1 and not calls
    assert out['groups'][0]['errorCode']=='OFFICIAL_MEMBER_COLLECTION_CHANGED'


def test_recovery_respects_the_shared_daily_limit(database,tmp_path,monkeypatch):
    _,f=database;c,_=seed(f,tmp_path)
    from backend.tests.macro.test_official_daily_budget_switch import fill_day
    fill_day(f,tmp_path,total=3)
    c=replace(c,official_policy=c.official_policy.model_copy(update={'dailyBudgetEnabled':True}))
    m=import_module('app.macro.jobs.issuer_sample_runtime');monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:c)
    before=count(f,JobCursor)
    out=m.execute_issuer_samples(f,c,clock=lambda:NOW+timedelta(seconds=2),recovery=True)
    assert out['supplierHttp']==0 and count(f,JobCursor)==before and out['groups'][0]['status']=='budget_pending'
