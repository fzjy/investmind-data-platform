from copy import deepcopy
from datetime import timedelta
from importlib import import_module
import httpx
import pytest
from sqlalchemy import select
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_housing_history import config
from backend.tests.macro.test_tier1_housing_discovery import directory,REPORT,LABEL,NOW


def recovery():
    try:return import_module('app.macro.housing_history_recovery')
    except ModuleNotFoundError:pytest.fail('named housing recovery not implemented')


def failed(factory,monkeypatch):
    runtime=import_module('app.macro.jobs.housing_history_runtime');c=config();monkeypatch.setattr(runtime,'load_extension_config',lambda:c)
    def timeout(_):raise httpx.ReadTimeout('timeout')
    r=runtime.execute_housing_history(factory,c,clock=lambda:NOW,transport=httpx.MockTransport(timeout))
    assert r['errorCode']=='EXTENSION_TIMEOUT'
    with factory() as s:p=s.get(JobRun,r['runId']);before=deepcopy(p.details)
    return runtime,c,r['runId'],before


def test_named_single_attempt_preserves_parent_and_unblocks_only_completed_recovery(database,monkeypatch):
    m=recovery();_,factory=database;runtime,c,parent,before=failed(factory,monkeypatch);calls=[]
    def handler(request):calls.append(str(request.url));return response(200,content=directory([(REPORT,LABEL)]))
    now=NOW+timedelta(minutes=2)
    r=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(handler))
    assert r['status']=='completed' and r['supplierHttp']==1 and calls==['https://www.stats.gov.cn/xxgk/jd/sjjd2020/']
    again=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:pytest.fail('must not resend')))
    assert again['supplierHttp']==0 and again['readOnly']
    with factory() as s:
        old=s.get(JobRun,parent);assert old.details==before and old.status=='failed' and old.error_message=='EXTENSION_TIMEOUT'
    p=runtime.directory_plan(factory,c,now)
    from app.macro.official_macro_store import OfficialMacroStore
    state=OfficialMacroStore(factory).inspect(p,now)
    assert state['documents'][0]['phase']=='completed' and state['reservedCount']==1
    docs=runtime.completed_documents(factory,p,now);assert docs[0]['recovery']['parentRunId']==parent and docs[0]['runId']!=parent
    assert m.reserved_total(factory)==2


def test_recovery_failure_is_frozen_and_cannot_be_recovered_again(database,monkeypatch):
    m=recovery();_,factory=database;_,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    def timeout(_):raise httpx.ReadTimeout('timeout')
    first=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(timeout))
    again=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert first['supplierHttp']==1 and again['supplierHttp']==0 and again['status']=='blocked'
    nested=m.execute_recovery(factory,c,first['runId'],clock=lambda:now,transport=httpx.MockTransport(lambda _:pytest.fail('no nested recovery')))
    assert nested['status']=='blocked' and nested['supplierHttp']==0


def test_unknown_reservation_never_qualifies_for_recovery(database,monkeypatch):
    m=recovery();_,factory=database;runtime=import_module('app.macro.jobs.housing_history_runtime');c=config()
    p=runtime.directory_plan(factory,c,NOW)
    from app.macro.official_macro_store import OfficialMacroStore
    parent=OfficialMacroStore(factory).reserve(p,p['documents'][0],NOW)
    r=m.execute_recovery(factory,c,parent,clock=lambda:NOW+timedelta(minutes=2),transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert r['status']=='blocked' and r['supplierHttp']==0 and count(factory,SourcePayload)==0


def test_parent_mutation_after_recovery_is_not_hidden(database,monkeypatch):
    m=recovery();_,factory=database;runtime,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    assert m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:response(200,content=directory())))['status']=='completed'
    with factory.begin() as s:s.get(JobRun,parent).error_message='EXTENSION_NETWORK_ERROR'
    p=runtime.directory_plan(factory,c,now)
    from app.macro.official_macro_store import OfficialMacroStore
    with pytest.raises(ValueError):OfficialMacroStore(factory).inspect(p,now)


def test_received_recovery_replays_without_another_http(database,monkeypatch):
    m=recovery();_,factory=database;_,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    import app.macro.official_macro_store as stores
    original=stores.normalize_document
    def broken(*_):raise ValueError('parser temporarily broken')
    monkeypatch.setattr(stores,'normalize_document',broken)
    first=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:response(200,content=directory())))
    assert first['status']=='replay_pending' and first['supplierHttp']==1
    monkeypatch.setattr(stores,'normalize_document',original)
    again=m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:pytest.fail('received must replay')))
    assert again['status']=='completed' and again['supplierHttp']==0


def test_total_budget_exhaustion_blocks_before_http(database,monkeypatch):
    m=recovery();_,factory=database;_,c,parent,_=failed(factory,monkeypatch)
    monkeypatch.setattr(m,'reserved_in_session',lambda _:c.historyBackfills['tier1Housing'].maxHttpTotal)
    r=m.execute_recovery(factory,c,parent,clock=lambda:NOW+timedelta(minutes=2),transport=httpx.MockTransport(lambda _:pytest.fail('budget exhausted')))
    assert r['status']=='blocked' and r['supplierHttp']==0
    with factory() as s:assert s.get(JobRun,m.recovery_key(parent)) is None


def test_dry_run_and_collection_disabled_during_rate_wait_do_not_reserve(database,monkeypatch):
    m=recovery();_,factory=database;runtime,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    r=m.execute_recovery(factory,c,parent,clock=lambda:now,dry_run=True)
    assert r['readOnly'] and r['supplierHttp']==0 and r['phase']=='new'
    class Rate:
        def acquire(self):monkeypatch.setattr(runtime,'load_extension_config',lambda:c.model_copy(update={'historyBackfills':{}}))
    r=m.execute_recovery(factory,c,parent,clock=lambda:now,rate_limiter=Rate(),transport=httpx.MockTransport(lambda _:pytest.fail('disabled')))
    assert r['status']=='blocked' and r['supplierHttp']==0
    with factory() as s:assert s.get(JobRun,m.recovery_key(parent)) is None


def test_auth_failure_is_not_eligible(database,monkeypatch):
    m=recovery();_,factory=database;_,c,parent,_=failed(factory,monkeypatch)
    with factory.begin() as s:s.get(JobRun,parent).error_message='EXTENSION_AUTH_ERROR'
    r=m.execute_recovery(factory,c,parent,clock=lambda:NOW+timedelta(minutes=2),transport=httpx.MockTransport(lambda _:pytest.fail('auth forbidden')))
    assert r['status']=='blocked' and r['supplierHttp']==0


def test_recovered_directory_and_report_publish_from_actual_bound_raw(database,monkeypatch):
    m=recovery();_,factory=database;runtime,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    assert m.execute_recovery(factory,c,parent,clock=lambda:now,transport=httpx.MockTransport(lambda _:response(200,content=directory([(REPORT,LABEL)]))))['status']=='completed'
    def handler(request):
        if str(request.url).endswith('index_1.html'):return response(200,content=directory([],1))
        raise httpx.ReadTimeout('report timeout')
    r=runtime.execute_housing_history(factory,c,clock=lambda:now+timedelta(seconds=10),transport=httpx.MockTransport(handler))
    assert r['supplierHttp']==2 and r['errorCode']=='EXTENSION_TIMEOUT'
    from backend.tests.macro.test_official_tier1_housing import document
    report_parent=r['runId'];assert report_parent!=parent
    assert m.execute_recovery(factory,c,report_parent,clock=lambda:now+timedelta(seconds=20),transport=httpx.MockTransport(lambda _:response(200,content=document())))['status']=='completed'
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,now+timedelta(seconds=21))
    g=view['groups']['tier1Housing']
    assert g['series'][0]['latest']==dict(month='2026-09-01',value='-0.4')
    assert g['plan']['directoryDocuments'][0]['recovery']['parentRunId']==parent
    assert g['documents'][0]['recovery']['parentRunId']==report_parent
    assert m.reserved_total(factory)==5
    again=runtime.execute_housing_history(factory,c,clock=lambda:now+timedelta(seconds=22),transport=httpx.MockTransport(lambda _:pytest.fail('same target')))
    assert again['status']=='completed' and again['supplierHttp']==0


def test_second_reservation_cannot_authorize_another_send(database,monkeypatch):
    m=recovery();_,factory=database;_,c,parent,_=failed(factory,monkeypatch);now=NOW+timedelta(minutes=2)
    from app.macro.official_macro_store import OfficialMacroStore
    store=OfficialMacroStore(factory);m.prepare(store,parent,now)
    with pytest.raises(ValueError,match='ALREADY_RESERVED'):m.prepare(store,parent,now)
    assert m.reserved_total(factory)==2


@pytest.mark.parametrize('side',['directory','report'])
def test_public_parent_evidence_hash_replacement_is_rejected(side):
    from backend.tests.e2e.export_controlled_housing_history import cases
    from app.macro.official_macro_view import build_official_macro_view
    v=cases()['recovered'];g=v['groups']['tier1Housing']
    d=g['plan']['directoryDocuments'][0] if side=='directory' else g['documents'][0]
    d['recovery']['parentEvidenceHash']='c'*64
    if side=='directory':
        from app.macro.extensions.plan import digest
        p=g['plan'];p['fingerprint']=digest({k:v for k,v in p.items() if k!='fingerprint'})
    groups={k:{n:x for n,x in group.items() if n not in ('series','progress')} for k,group in v['groups'].items()}
    with pytest.raises(ValueError):build_official_macro_view(dict(analysisDate=v['analysisDate'],groups=groups))
