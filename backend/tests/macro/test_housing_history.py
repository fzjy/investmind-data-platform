"""历史目录/报告是实际捕获的独立计划，不伪造过去周计划。"""
from copy import deepcopy
from datetime import timedelta
from importlib import import_module
import httpx
import pytest

from app.db.models.core import MacroObservation
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_tier1_housing_discovery import directory,REPORT,LABEL,NOW
from backend.tests.macro.test_official_tier1_housing import document


def module():
    try:return import_module('app.macro.housing_history_plan')
    except ModuleNotFoundError:pytest.fail('housing history not implemented')


def policy():
    return dict(collectionEnabled=True,maxDirectoryPages=2,maxHttpPerRun=3,maxHttpTotal=50,requestsPerMinute=10,
        requestTimeoutSeconds=20,maxRetries=0,maxResponseBytes=5242880,maxResponseRows=16)


def plan(known=()):
    return module().build_directory_plan(policy(),NOW,'2022-10-01','2026-09-01',list(known))


def test_directory_history_collects_multiple_months_from_actual_labels_only():
    m=module();p=plan();body=directory([(REPORT,LABEL),(REPORT.replace('1965610','1965611'),LABEL.replace('9月份','8月份'))])
    n=m.parse_history_directory(body,p,0)
    assert [c['period'] for c in n['candidates']]==['2026-08','2026-09']
    assert n['page']==0 and n['source']=='nbs' and n['recordCount']==500


@pytest.mark.parametrize('change',[
    lambda p:p.update(maxDirectoryPages=51),lambda p:p.update(maxHttpPerRun=4),lambda p:p.update(maxRetries=1),
    lambda p:p.update(maxHttpTotal=1),lambda p:p.update(requestsPerMinute=11),lambda p:p.update(extra=True),
])
def test_history_budget_is_closed(change):
    p=policy();change(p)
    with pytest.raises(ValueError):module().HistoryPolicy.model_validate(p)


def test_history_plan_has_actual_capture_time_and_rehash_cannot_change_source_pages():
    m=module();p=plan(['2026-09'])
    assert p['scheduledAt']==NOW.isoformat() and len(p['documents'])==2
    assert p['knownPeriods']==['2026-09']
    m.validate_history_plan(p)
    bad=deepcopy(p);bad['documents'][1]['url']='https://example.org/'
    from app.macro.extensions.plan import digest
    bad['fingerprint']=digest({k:v for k,v in bad.items() if k!='fingerprint'})
    with pytest.raises(ValueError):m.validate_history_plan(bad)


def config():
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    value=load_extension_config().model_dump();value['historyBackfills']={'tier1Housing':policy()}
    return ExtensionConfig.model_validate(value)


@pytest.mark.parametrize('failed',[False,True])
def test_three_request_checkpoint_then_resume_or_failed_freeze(database,monkeypatch,failed):
    m=module();_,factory=database
    runtime=import_module('app.macro.jobs.housing_history_runtime');c=config()
    monkeypatch.setattr(runtime,'load_extension_config',lambda:c)
    calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url))
        if failed:return response(403)
        if i<2:return response(200,content=directory([(REPORT,LABEL)] if i==0 else [(REPORT.replace('1965610','1965611'),LABEL.replace('9月份','8月份'))],page=i))
        period='2026-08' if '1965611' in str(request.url) else '2026-09'
        return response(200,content=document(title=f'{period[:4]}年{int(period[5:])}月份商品住宅销售价格变动情况解读'))
    clock=lambda:NOW+timedelta(seconds=len(calls)*3+1)
    first=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    second=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    third=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    if failed:
        assert first['supplierHttp']==1 and second['supplierHttp']==third['supplierHttp']==0
        assert second['status']=='blocked' and count(factory,MacroObservation)==0
    else:
        assert [(r['status'],r['supplierHttp']) for r in (first,second,third)]==[('partial',3),('completed',1),('completed',0)]
        assert count(factory,MacroObservation)==4 and len(calls)==4


def test_dry_run_is_no_network_no_writes(database):
    module();_,factory=database;r=import_module('app.macro.jobs.housing_history_runtime').execute_housing_history(
        factory,config(),clock=lambda:NOW,dry_run=True,transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert r['readOnly'] and r['supplierHttp']==0 and count(factory,MacroObservation)==0


def test_completed_history_is_visible_with_real_capture_time_and_not_a_fake_old_week(database,monkeypatch):
    m=module();_,factory=database
    runtime=import_module('app.macro.jobs.housing_history_runtime');c=config();monkeypatch.setattr(runtime,'load_extension_config',lambda:c)
    calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url))
        return response(200,content=directory([(REPORT,LABEL)],i) if i<2 else document())
    clock=lambda:NOW+timedelta(seconds=len(calls)*3+1)
    assert runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))['status']=='completed'
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,clock())
    group=view['groups']['tier1Housing']
    assert group['series'][0]['latest']==dict(month='2026-09-01',value='-0.4')
    assert group['plan']['normalizerVersion']==m.REPORT_VERSION
    assert group['plan']['scheduledAt']==(NOW+timedelta(seconds=1)).isoformat()
    assert group['documents'][0]['retrievedAt']>group['plan']['scheduledAt']


def test_reservation_uncertain_is_not_reported_readonly_and_never_resends(database,monkeypatch):
    module();_,factory=database;runtime=import_module('app.macro.jobs.housing_history_runtime');c=config()
    monkeypatch.setattr(runtime,'load_extension_config',lambda:c)
    from app.macro.official_macro_store import OfficialMacroStore
    original=OfficialMacroStore.reserve
    def uncertain(self,*args):original(self,*args);raise RuntimeError('commit result uncertain')
    monkeypatch.setattr(OfficialMacroStore,'reserve',uncertain)
    first=runtime.execute_housing_history(factory,c,clock=lambda:NOW,transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert first['status']=='blocked' and first['readOnly'] is False and first['supplierHttp']==0
    again=runtime.execute_housing_history(factory,c,clock=lambda:NOW,transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert again['status']=='blocked' and again['supplierHttp']==0


def test_collection_disabled_during_rate_wait_stops_before_reservation(database,monkeypatch):
    module();_,factory=database;runtime=import_module('app.macro.jobs.housing_history_runtime');c=config();current=[c]
    from app.macro.extensions.config import ExtensionConfig
    disabled=c.model_dump();disabled['historyBackfills']['tier1Housing']['collectionEnabled']=False
    class Limiter:
        def acquire(self):current[0]=ExtensionConfig.model_validate(disabled)
    monkeypatch.setattr(runtime,'load_extension_config',lambda:current[0])
    r=runtime.execute_housing_history(factory,c,clock=lambda:NOW,rate_limiter=Limiter(),transport=httpx.MockTransport(lambda _:response(403)))
    assert r['supplierHttp']==0 and r['readOnly'] and r['status']=='blocked'


def test_known_real_month_is_not_refetched_and_history_can_coexist_with_legacy_static_group(database,monkeypatch):
    m=module();_,factory=database
    from app.macro.official_macro_store import OfficialMacroStore
    from backend.tests.macro.test_official_macro_config import policy as ordinary_policy
    from app.macro.official_macro_plan import build_official_macro_plan
    ordinary=build_official_macro_plan('tier1Housing',ordinary_policy('tier1Housing'),NOW)
    store=OfficialMacroStore(factory);key=store.reserve(ordinary,ordinary['documents'][0],NOW);store.receive(key,document(),NOW);store.replay(key,NOW)
    runtime=import_module('app.macro.jobs.housing_history_runtime');c=config();monkeypatch.setattr(runtime,'load_extension_config',lambda:c);calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url))
        if i<2:return response(200,content=directory([(REPORT,LABEL),(REPORT.replace('1965610','1965611'),LABEL.replace('9月份','8月份'))] if i==0 else [],i))
        assert str(request.url)!=REPORT
        return response(200,content=document(title='2026年8月份商品住宅销售价格变动情况解读'))
    clock=lambda:NOW+timedelta(seconds=len(calls)*3+2)
    r=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    assert r['status']=='completed' and r['supplierHttp']==3
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,clock())
    g=view['groups']['tier1Housing'];assert len(g['history'])==1
    assert g['history'][0]['plan']['knownPeriods']==['2026-09']
    assert g['series'][0]['points'][-2:]==[dict(month='2026-08-01',value='-0.4'),dict(month='2026-09-01',value='-0.4')]


def test_report_reservation_requires_actual_directory_runs_and_raw_not_just_self_consistent_json(database):
    from backend.tests.e2e.export_controlled_housing_history import cases
    from app.macro.official_macro_store import OfficialMacroStore
    from datetime import datetime
    _,factory=database;p=cases()['standalone']['groups']['tier1Housing']['plan']
    with pytest.raises(ValueError):OfficialMacroStore(factory).reserve(p,p['documents'][0],datetime.fromisoformat(p['scheduledAt'])+timedelta(seconds=10))
    assert count(factory,MacroObservation)==0


@pytest.mark.parametrize('conflict',['url','count'])
def test_source_conflict_blocks_only_history_and_retains_normal_published_inputs(database,monkeypatch,conflict):
    module();_,factory=database
    from app.macro.official_macro_store import OfficialMacroStore
    from app.macro.official_macro_plan import build_official_macro_plan
    from backend.tests.macro.test_official_macro_config import policy as ordinary_policy
    p=build_official_macro_plan('tier1Housing',ordinary_policy('tier1Housing'),NOW);store=OfficialMacroStore(factory)
    key=store.reserve(p,p['documents'][0],NOW);store.receive(key,document(),NOW);store.replay(key,NOW)
    runtime=import_module('app.macro.jobs.housing_history_runtime');c=config();monkeypatch.setattr(runtime,'load_extension_config',lambda:c);calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url));assert i<2
        url=REPORT.replace('1965610','1965611') if conflict=='url' and i==1 else REPORT
        body=directory([(url,LABEL.replace('9月份','8月份'))],i)
        if conflict=='count' and i==1:body=body.replace(b'"500"',b'"501"')
        return response(200,content=body)
    clock=lambda:NOW+timedelta(seconds=len(calls)*3+2)
    r=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    assert r['status']=='blocked' and r['supplierHttp']==2
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,clock())
    assert view['groups']['tier1Housing']['series'][0]['latest']==dict(month='2026-09-01',value='-0.4')
    assert count(factory,MacroObservation)==2
    again=runtime.execute_housing_history(factory,c,clock=clock,transport=httpx.MockTransport(handler))
    assert again['status']=='blocked' and again['supplierHttp']==0
