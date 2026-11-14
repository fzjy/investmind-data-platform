"""专项债月报目录：精确月份、持久预算及跨周完整历史。"""
from copy import deepcopy
from datetime import timedelta,datetime,timezone
from importlib import import_module
from html import escape
import httpx
import pytest
from app.macro.official_macro_plan import build_official_macro_plan
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_special_bonds import monthly,quota
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from app.db.models import MacroObservation,JobRun

DIRECTORY='https://zwgls.mof.gov.cn/tjsj/'
REPORT='https://zwgls.mof.gov.cn/tjsj/202610/t20261027_4003589.htm'


def discovery_policy():
    p=policy('specialBonds');annual=p['documents'][0]
    p['documents']=[dict(id='special-directory',role='special-directory',period='latest',url=DIRECTORY,derivedFrom=None),
        dict(id='special-report',role='special-issuance',period='latest-completed-month',url=None,derivedFrom='special-directory'),annual]
    return p


def plan(now=NOW):return build_official_macro_plan('specialBonds',discovery_policy(),now)


def directory(links=(),title='统计数据'):
    return ('<html><title>'+title+'</title>'+''.join('<a href="'+escape(h,quote=True)+'">'+escape(t)+'</a>' for h,t in links)+'</html>').encode()


def parse(body=None):
    try:fn=import_module('app.macro.special_discovery').parse_special_directory
    except ModuleNotFoundError:pytest.fail('special directory not implemented')
    return fn(body or directory([(REPORT,'2026年9月地方政府债券发行和债务余额情况')]),'2026-09',max_candidates=2)


def test_exact_report_period_and_derived_identity_without_changing_three_slot_budget():
    p=plan();assert p['limits']['maxHttpPerPeriod']==3
    value=parse();assert value['selected']==dict(period='2026-09',url=REPORT)
    assert value['role']=='special-directory' and value['source']=='mof' and value['candidateCount']==1


@pytest.mark.parametrize('href',[REPORT.replace('zwgls.mof.gov.cn','example.org'),REPORT+'?token=x',REPORT+'#part',
    '//example.org/report.htm','../202610/t20261027_4003589.htm','./202610/%7420261027_4003589.htm'])
def test_matching_label_with_untrusted_url_rejected(href):
    with pytest.raises(ValueError):parse(directory([(href,'2026年9月地方政府债券发行和债务余额情况')]))


def test_duplicate_same_link_accepted_different_links_conflict_and_wrong_month_not_selected():
    label='2026年9月地方政府债券发行和债务余额情况'
    assert parse(directory([(REPORT,label),(REPORT,label)]))['candidateCount']==1
    with pytest.raises(ValueError):parse(directory([(REPORT,label),(REPORT.replace('4003589','4003590'),label)]))
    assert parse(directory([(REPORT,label.replace('9月','8月'))]))['selected'] is None
    with pytest.raises(ValueError):parse(directory([(REPORT,label)],title='arbitrary page'))


def test_cross_year_requires_correct_annual_quota_not_old_year_constant():
    with pytest.raises(ValueError):plan(datetime(2027,2,15,6,tzinfo=timezone.utc))


@pytest.mark.parametrize('found',[False,True])
def test_normal_executor_three_http_or_bounded_absence_zero_reentry(database,monkeypatch,found):
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    from app.macro.jobs import official_macro_runtime as runtime
    from app.macro.official_macro_view import load_official_macro_view,official_macro_view_valid
    from app.macro.official_macro_store import OfficialMacroStore
    _,f=database;value=load_extension_config().model_dump();value['groups']['specialBonds']=discovery_policy()
    value['groups']['specialBonds']['collectionEnabled']=True;c=ExtensionConfig.model_validate(value)
    monkeypatch.setattr(runtime,'load_extension_config',lambda:c);calls=[]
    def handler(request):
        calls.append(str(request.url))
        body=directory([(REPORT,'2026年9月地方政府债券发行和债务余额情况')]) if str(request.url)==DIRECTORY else monthly(month=9,ytd='35608',direct='6339') if str(request.url)==REPORT else quota()
        if not found and str(request.url)==DIRECTORY:body=directory([])
        return response(200,content=body)
    transport=httpx.MockTransport(handler);clock=lambda:NOW+timedelta(minutes=1)
    first=runtime.execute_official_macro_group(f,c,'specialBonds',transport=transport,clock=clock)
    assert first['supplierHttp']==(3 if found else 1)
    assert first['status']==('completed' if found else 'source_pending')
    assert count(f,MacroObservation)==(3 if found else 0)
    second=runtime.execute_official_macro_group(f,c,'specialBonds',transport=transport,clock=clock)
    assert second['supplierHttp']==0 and len(calls)==(3 if found else 1)
    assert OfficialMacroStore(f).inspect(plan(),clock())['status']==('completed' if found else 'source_pending')
    view=load_official_macro_view(f,clock());assert official_macro_view_valid(view)


def test_failed_directory_remains_frozen_in_new_week(database,monkeypatch):
    from app.macro.official_macro_store import OfficialMacroStore
    _,f=database;s=OfficialMacroStore(f);p=plan();key=s.reserve(p,p['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    state=s.inspect(plan(NOW+timedelta(days=14)),NOW+timedelta(days=14))
    assert state['status']=='blocked' and state['plan']==p and count(f,JobRun)==1


def test_public_history_preserves_prior_months_and_cross_vintage_discrepancy():
    from backend.tests.e2e.export_controlled_special_discovery import cases
    from app.macro.official_macro_view import official_macro_view_valid
    views=cases();assert all(official_macro_view_valid(v) for v in views.values())
    g=views['withHistory']['groups']['specialBonds']
    assert g['series'][0]['latest']==dict(month='2026-10-01',value='4392')
    assert g['progress']['crossVintageDifference']=='4392'
    assert g['progress']['computedProgress'].startswith('90.909')
    assert g['history'][0]['progress']['differenceDiscrepancy']=='24'


@pytest.mark.parametrize('change',['selected-url','source','target','budget','derived-parent','nested-history','reversed-history-time','reported-value'])
def test_rehashed_public_forgery_is_rejected(change):
    from backend.tests.e2e.export_controlled_special_discovery import cases
    from app.macro.extensions.plan import digest
    from app.macro.official_macro_view import official_macro_view_valid
    value=deepcopy(cases()['withHistory']);g=value['groups']['specialBonds'];doc=g['documents'][0]
    if change=='selected-url':doc['normalized']['selected']['url']='https://example.org/report.htm'
    elif change=='source':doc['normalized']['source']='pboc'
    elif change=='target':doc['normalized']['targetMonth']='2026-09'
    elif change=='budget':g['plan']['limits']['maxHttpPerPeriod']=4
    elif change=='derived-parent':g['documents'][1]['request']['directoryRunId']=g['documents'][2]['runId']
    elif change=='nested-history':g['history'][0]['history']=[]
    elif change=='reversed-history-time':
        g['history'][0]['documents'][0]['completedAt']=(datetime.fromisoformat(g['documents'][-1]['completedAt'])+timedelta(seconds=1)).isoformat()
    else:g['history'][0]['progress']['differenceDiscrepancy']='0'
    value['evidenceHash']=digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not official_macro_view_valid(value)


def test_persisted_new_week_keeps_old_complete_root_and_same_week_no_http(database):
    from app.macro.official_macro_store import OfficialMacroStore
    from app.macro.official_macro_view import load_official_macro_view,official_macro_view_valid
    from backend.tests.macro.test_official_special_bonds import actual_briefing
    _,f=database;s=OfficialMacroStore(f)
    old=build_official_macro_plan('specialBonds',policy('specialBonds'),NOW)
    for i,(doc,body) in enumerate(zip(old['documents'],[quota(),monthly(),actual_briefing()])):
        start=NOW+timedelta(seconds=i*3);key=s.reserve(old,doc,start)
        s.receive(key,body,start+timedelta(seconds=1));s.replay(key,start+timedelta(seconds=2))
    when=NOW+timedelta(days=14);p=plan(when)
    for i,(doc,body) in enumerate(zip(p['documents'],[
        directory([(REPORT,'2026年10月地方政府债券发行和债务余额情况')]),monthly(month=10,ytd='40000',direct='4392'),quota()])):
        start=when+timedelta(seconds=i*3);key=s.reserve(p,doc,start)
        s.receive(key,body,start+timedelta(seconds=1));s.replay(key,start+timedelta(seconds=2))
    view=load_official_macro_view(f,when+timedelta(minutes=1));assert official_macro_view_valid(view)
    g=view['groups']['specialBonds'];assert len(g['history'])==1
    assert g['history'][0]['progress']['differenceDiscrepancy']=='24'
    assert g['progress']['directMonth']=='4392'
    assert [r['value'] for r in g['series'][0]['points'] if r['value'] is not None]==['5213','6339','4392']
    assert s.inspect(p,when+timedelta(minutes=2))['status']=='completed'


def test_january_monthly_history_does_not_collide_with_annual_quota(database):
    from app.macro.official_macro_store import OfficialMacroStore
    from app.macro.official_macro_view import load_official_macro_view
    _,f=database;s=OfficialMacroStore(f)
    for month,when,ytd,direct in [(1,datetime(2026,2,15,6,tzinfo=timezone.utc),'100','100'),
        (2,datetime(2026,3,15,6,tzinfo=timezone.utc),'300','200')]:
        p=plan(when)
        bodies=[directory([(REPORT,f'2026年{month}月地方政府债券发行和债务余额情况')]),monthly(month,ytd,direct),quota()]
        for i,(doc,body) in enumerate(zip(p['documents'],bodies)):
            start=when+timedelta(seconds=i*3);key=s.reserve(p,doc,start)
            s.receive(key,body,start+timedelta(seconds=1));s.replay(key,start+timedelta(seconds=2))
    g=load_official_macro_view(f,when+timedelta(minutes=1))['groups']['specialBonds']
    assert next(p for p in g['series'][0]['points'] if p['month']=='2026-01-01')['value']=='100'
    assert g['progress']['crossVintageDifference']=='200'


def test_derived_report_time_cannot_precede_parent_completion():
    from backend.tests.e2e.export_controlled_special_discovery import cases
    from app.macro.extensions.plan import digest
    from app.macro.official_macro_view import official_macro_view_valid
    value=deepcopy(cases()['withHistory']);parent,child=value['groups']['specialBonds']['documents'][:2]
    child['retrievedAt']=child['completedAt']=parent['retrievedAt']
    value['evidenceHash']=digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not official_macro_view_valid(value)
