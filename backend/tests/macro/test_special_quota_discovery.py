from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module
from html import escape
import pytest
from backend.tests.macro.test_special_discovery import discovery_policy as monthly_policy,directory as monthly_directory,REPORT
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_official_special_bonds import quota,monthly
from app.macro.official_macro_plan import build_official_macro_plan
from app.macro.official_macro_store import OfficialMacroStore
from app.db.models import MacroObservation,JobRun

ROOT='https://www.mof.gov.cn/zyyjsgkpt/zyzfyjs/zyys/ysbb/'
YEAR='https://yss.mof.gov.cn/2026zyczys/'
QUOTA='https://yss.mof.gov.cn/2026zyczys/202603/t20260324_3985998.htm'

def page(title,links=()):return ('<html><title>'+title+'</title>'+''.join('<a href="'+escape(u,quote=True)+'">'+escape(t)+'</a>' for u,t in links)+'</html>').encode()
def root_page(year=2026):return page('预算报表',[(YEAR.replace('2026',str(year)),f'{year}年中央财政预算')])
def year_page(year=2026):return page(f'{year}年中央财政预算',[(QUOTA.replace('2026',str(year)),f'{year-1}年和{year}年地方政府专项债务余额情况表')])
def policy():
    p=monthly_policy();p['limits']['maxHttpPerPeriod']=5
    p['documents']=[dict(id='budget-directory',role='special-budget-directory',period='latest',url=ROOT,derivedFrom=None),
        dict(id='budget-year',role='special-year-directory',period='target-year',url=None,derivedFrom='budget-directory'),
        dict(id='special-quota',role='special-quota',period='target-year',url=None,derivedFrom='budget-year'),*p['documents'][:2]]
    return p
def plan(now=NOW):return build_official_macro_plan('specialBonds',policy(),now)
def parse(body,role='special-budget-directory',year=2026):
    try:fn=import_module('app.macro.special_quota_discovery').parse_quota_directory
    except ModuleNotFoundError:pytest.fail('annual quota discovery missing')
    return fn(body,role,year,max_candidates=2)

def test_exact_year_and_semantic_special_table_selected():
    assert parse(root_page())['selected']==dict(year='2026',url=YEAR)
    assert parse(year_page(),'special-year-directory')['selected']==dict(year='2026',url=QUOTA)
    assert parse(root_page(2025))['selected'] is None
    assert parse(page('预算报表',[(YEAR.replace('https:','http:'),'2026年中央财政预算')]))['selected']['url']==YEAR
    assert parse(page('2026年中央财政预算',[(QUOTA,'2025年和2026年地方政府一般债务余额情况表')]),'special-year-directory')['selected'] is None

@pytest.mark.parametrize('url',[QUOTA.replace('yss.mof.gov.cn','example.org'),QUOTA+'?token=1',QUOTA+'#x',QUOTA.replace('2026zyczys','2025zyczys'),QUOTA.replace('https:','http:'),'../202603/t20260324_3985998.htm'])
def test_wrong_source_year_or_unsafe_url_rejected(url):
    with pytest.raises(ValueError):parse(page('2026年中央财政预算',[(url,'2025年和2026年地方政府专项债务余额情况表')]),'special-year-directory')

def test_ambiguity_title_and_budget_fail_closed():
    with pytest.raises(ValueError):parse(page('预算报表',[(YEAR,'2026年中央财政预算'),(YEAR+'index.htm','2026年中央财政预算')]))
    with pytest.raises(ValueError):parse(page('unrelated',[(YEAR,'2026年中央财政预算')]))
    assert plan()['limits']['maxHttpPerRun']==3 and len(plan()['documents'])==5
    cross=plan(datetime(2027,2,15,6,tzinfo=timezone.utc));assert cross['endPeriod']=='2027-01-01'
    assert all('2026' not in str(d) for d in cross['documents'])

def complete(f,p,doc,body,when):
    s=OfficialMacroStore(f);key=s.reserve(p,doc,when);s.receive(key,body,when+timedelta(seconds=1));s.replay(key,when+timedelta(seconds=2));return key

def test_five_node_persisted_chain_and_dynamic_quota_year(database):
    _,f=database;p=plan();s=OfficialMacroStore(f)
    bodies=[root_page(),year_page(),quota(),monthly_directory([(REPORT,'2026年9月地方政府债券发行和债务余额情况')]),monthly(9,'35608','6339')]
    for i,(doc,body) in enumerate(zip(p['documents'],bodies)):
        complete(f,p,doc,body,NOW+timedelta(seconds=i*3))
    state=s.inspect(p,NOW+timedelta(minutes=1));assert state['status']=='completed' and state['reservedCount']==5
    assert count(f,MacroObservation)==3
    from app.macro.official_macro_view import load_official_macro_view,official_macro_view_valid
    view=load_official_macro_view(f,NOW+timedelta(minutes=1));assert official_macro_view_valid(view)
    assert view['groups']['specialBonds']['progress']['quota']=='44000'

@pytest.mark.parametrize('missing',[0,1,3])
def test_missing_annual_or_month_candidate_is_terminal_prefix(database,missing):
    _,f=database;p=plan();bodies=[root_page(),year_page(),quota(),monthly_directory([])]
    bodies[missing]=page('预算报表' if missing==0 else '2026年中央财政预算' if missing==1 else '统计数据')
    for i in range(missing+1):complete(f,p,p['documents'][i],bodies[i],NOW+timedelta(seconds=i*3))
    s=OfficialMacroStore(f);state=s.inspect(p,NOW+timedelta(minutes=1));assert state['status']=='source_pending'
    assert state['reservedCount']==missing+1

def test_migration_retains_terminal_old_cycle_but_never_bypasses_failure(database):
    from backend.tests.macro.test_special_discovery import plan as old_plan
    _,f=database;s=OfficialMacroStore(f);old=old_plan()
    complete(f,old,old['documents'][0],monthly_directory([]),NOW)
    state=s.inspect(plan(),NOW+timedelta(minutes=1));assert state['plan']==old and state['status']=='source_pending'
    # 下个真实计划才使用新图；测试时钟仅用于隔离输入。
    later=NOW+timedelta(days=14);assert s.inspect(plan(later),later)['status']=='new'

@pytest.mark.parametrize('missing',[None,0,1,3])
def test_normal_runtime_obeys_three_per_run_five_per_period_and_zero_reentry(database,monkeypatch,missing):
    import httpx
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    from app.macro.jobs import official_macro_runtime as runtime
    from backend.tests.macro.test_extension_client import response
    _,f=database;raw=load_extension_config().model_dump();raw['groups']['specialBonds']=policy();raw['groups']['specialBonds']['collectionEnabled']=True
    config=ExtensionConfig.model_validate(raw);monkeypatch.setattr(runtime,'load_extension_config',lambda:config)
    calls=[];bodies={ROOT:root_page(),YEAR:year_page(),QUOTA:quota(),'https://zwgls.mof.gov.cn/tjsj/':monthly_directory([(REPORT,'2026年9月地方政府债券发行和债务余额情况')]),REPORT:monthly(9,'35608','6339')}
    if missing is not None:
        url=[ROOT,YEAR,QUOTA,'https://zwgls.mof.gov.cn/tjsj/'][missing]
        bodies[url]=page('预算报表' if missing==0 else '2026年中央财政预算' if missing==1 else '统计数据')
    def handler(r):calls.append(str(r.url));return response(200,content=bodies[str(r.url)])
    transport=httpx.MockTransport(handler);clock=lambda:NOW+timedelta(minutes=1)
    first=runtime.execute_official_macro_group(f,config,'specialBonds',transport=transport,clock=clock)
    assert first['supplierHttp']<=3
    second=runtime.execute_official_macro_group(f,config,'specialBonds',transport=transport,clock=clock)
    assert second['supplierHttp']<=3 and len(calls)==(5 if missing is None else missing+1)
    assert second['status']==('completed' if missing is None else 'source_pending')
    again=runtime.execute_official_macro_group(f,config,'specialBonds',transport=transport,clock=clock)
    assert again['supplierHttp']==0 and again['readOnly']

def test_annual_failed_or_partial_root_cannot_be_bypassed_by_next_year(database):
    _,f=database;s=OfficialMacroStore(f);p=plan();key=s.reserve(p,p['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    later=datetime(2027,2,15,6,tzinfo=timezone.utc)
    assert s.inspect(plan(later),later)['plan']==p
    assert s.inspect(plan(later),later)['status']=='blocked'

def test_old_same_cycle_failure_never_allows_annual_migration(database):
    from backend.tests.macro.test_special_discovery import plan as old_plan
    _,f=database;s=OfficialMacroStore(f);old=old_plan();key=s.reserve(old,old['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.inspect(plan(),NOW+timedelta(minutes=1))

def test_unpublished_next_year_never_falls_back_to_previous_quota(database):
    _,f=database;when=datetime(2027,2,15,6,tzinfo=timezone.utc);p=plan(when)
    complete(f,p,p['documents'][0],root_page(2026),when)
    state=OfficialMacroStore(f).inspect(p,when+timedelta(minutes=1))
    assert state['status']=='source_pending' and count(f,MacroObservation)==0 and count(f,JobRun)==1
    from app.macro.official_macro_view import load_official_macro_view,official_macro_view_valid
    v=load_official_macro_view(f,when+timedelta(minutes=1));assert official_macro_view_valid(v)
    assert v['groups']['specialBonds']['progress'] is None

@pytest.mark.parametrize('kind',['false-absence','monthly-before-annual'])
def test_public_annual_terminal_and_sequence_are_realizable(kind):
    from backend.tests.e2e.export_controlled_special_quota_discovery import cases
    from app.macro.official_macro_view import build_official_macro_view
    v=deepcopy(cases()['full']);g=v['groups']['specialBonds'];docs=g['documents']
    if kind=='false-absence':g['documents']=docs[:2];g['latestAttempt']['status']='source_pending'
    else:
        for i in (3,4):docs[i]['retrievedAt']=docs[i-3]['retrievedAt'];docs[i]['completedAt']=docs[i-3]['completedAt']
    inputs=dict(analysisDate=v['analysisDate'],groups={k:{key:value for key,value in group.items() if key not in ('series','progress')} for k,group in v['groups'].items()})
    with pytest.raises(ValueError):build_official_macro_view(inputs)
