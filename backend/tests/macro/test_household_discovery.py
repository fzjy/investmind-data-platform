"""目录发现只使用受控原件和内存库，不访问真实供应商。"""
from copy import deepcopy
from datetime import datetime,timedelta
from html import escape
from importlib import import_module

import httpx
import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW

DIRECTORY='https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'
REPORT=policy()['documents'][1]['url']


def directory(links=(), *, title='数据解读'):
    return ('<html><title>'+title+'</title>'+''.join('<a href="'+escape(url,quote=True)+'">'+escape(label)+'</a>'
        for url,label in links)+'</html>').encode()


def parse(body=None, month='2026-09', **kwargs):
    try:m=import_module('app.macro.household_discovery')
    except ModuleNotFoundError:pytest.fail('household directory discovery not implemented')
    return m.parse_household_directory(directory([(REPORT,'2026年前三季度金融统计数据报告')]) if body is None else body,
        month,max_candidates=kwargs.get('max_candidates',2))


def discovery_policy():
    p=policy()
    p['documents']=[dict(id='household-directory',role='household-directory',period='latest',url=DIRECTORY,derivedFrom=None),
        dict(id='household-report',role='household-ytd',period='latest-completed-month',url=None,derivedFrom='household-directory')]
    return p


def plan(p=None,now=NOW):
    return import_module('app.macro.official_macro_plan').build_official_macro_plan('householdCredit',p or discovery_policy(),now)


@pytest.mark.parametrize('month,title',[
    ('2026-09','2026年前三季度金融统计数据报告'),('2026-09','2026年9月份金融统计数据报告'),
    ('2026-06','2026年上半年金融统计数据报告'),('2026-03','2026年一季度金融统计数据报告'),
    ('2026-01','2026年1月金融统计数据报告'),('2025-12','2025年金融统计数据报告')])
def test_directory_uses_report_month_not_url_date(month,title):
    result=parse(directory([(REPORT,title)]),month)
    assert result['selected']==dict(period=month,url=REPORT)
    assert result['candidateCount']==1 and len(result['rawSha256'])==64 and 'points' not in result


def test_only_exact_target_and_no_old_or_future_month_fallback():
    result=parse(directory([(REPORT,'2026年8月金融统计数据报告'),(REPORT,'2026年10月金融统计数据报告'),
        (REPORT,'2026年前三季度地区社会融资规模增量统计表'),(REPORT,'2026年9月金融统计数据报告解读')]))
    assert result['selected'] is None and result['candidateCount']==0


def test_duplicates_same_link_are_one_candidate_and_script_links_are_ignored():
    body=directory([(REPORT,'2026年9月金融统计数据报告')])
    body+=('<script><a href="https://evil.example/">2026年前三季度金融统计数据报告</a></script>').encode()
    assert parse(body)['selected']['url']==REPORT
    assert parse(directory([(REPORT,'2026年前三季度金融统计数据报告')]*2))['candidateCount']==1


@pytest.mark.parametrize('url',[
    'https://evil.example/report','//evil.example/report','http://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026101417073743604/index.html',
    REPORT+'?x=1',REPORT+'#fragment',REPORT.replace('/202610','/../202610'),REPORT.replace('202610','%32202610'),
    REPORT.replace('www.pbc.gov.cn','www.pbc.gov.cn.evil.example'),REPORT.replace('https://','https://u@'),
    REPORT.replace('/index.html','\\index.html'),' '+REPORT,REPORT.replace('https://','https:\n//')])
def test_invalid_candidate_url_is_error_instead_of_no_candidate(url):
    with pytest.raises(ValueError,match='OFFICIAL_HOUSEHOLD_DIRECTORY_INVALID'):
        parse(directory([(url,'2026年前三季度金融统计数据报告')]))


def test_same_month_different_urls_are_ambiguous_and_never_selected_by_order():
    other=REPORT.replace('2026101417073743604','2026101417073743605')
    for links in ([(REPORT,'2026年前三季度金融统计数据报告'),(other,'2026年9月金融统计数据报告')],
        [(other,'2026年9月金融统计数据报告'),(REPORT,'2026年前三季度金融统计数据报告')]):
        with pytest.raises(ValueError):parse(directory(links))


def test_duplicate_href_attributes_cannot_hide_a_target_month_or_a_conflicting_link():
    body=directory([(REPORT,'2026年前三季度金融统计数据报告')])+(
        '<a href="'+REPORT.replace('2026101417073743604','2026101417073743605')+'" href="'+REPORT+'">2026年9月金融统计数据报告</a>').encode()
    with pytest.raises(ValueError):parse(body)
    body=('<title>数据解读</title><a href="'+REPORT+'" href="'+REPORT+'">2026年三季度金融机构贷款投向统计报告</a>').encode()
    with pytest.raises(ValueError):
        import_module('app.macro.official_macro_normalize').parse_property_directory(body,'2026-Q3',max_candidates=2,quarter_fallbacks=0)


@pytest.mark.parametrize('attrs',['','href','href=""'])
def test_missing_candidate_href_is_not_misreported_as_no_target(attrs):
    body=directory([(REPORT,'2026年9月金融统计数据报告')])+f'<a {attrs}>2026年前三季度金融统计数据报告</a>'.encode()
    with pytest.raises(ValueError):parse(body)


@pytest.mark.parametrize('body',[b'',b'<title>Access denied</title>',b'<title>Sign in</title>',
    '<title>数据解读</title><a href="x">未闭合'.encode()])
def test_bad_or_unrecognized_directory_does_not_complete_a_period(body):
    with pytest.raises(ValueError):parse(body)


@pytest.mark.parametrize('cap',[0,3,True])
def test_directory_limit_is_strict(cap):
    with pytest.raises(ValueError):parse(max_candidates=cap)


def test_configuration_and_plan_use_existing_two_slot_graph_without_new_defaults():
    module=import_module('app.macro.extensions.config')
    checked=module.validate_group('householdCredit',discovery_policy())
    assert checked.limits.maxHttpPerPeriod==2 and len(checked.documents)==2
    p=plan()
    assert p['documents']==discovery_policy()['documents'] and p['endPeriod']=='2026-09-01'
    import_module('app.macro.extensions.plan').validate_extension_plan(p)
    old=policy();assert module.validate_group('householdCredit',old).model_dump()==old
    assert set(p)==set(import_module('app.macro.official_macro_plan').build_official_macro_plan('householdCredit',old,NOW))


@pytest.mark.parametrize('mutation',[
    lambda p:p['documents'].reverse(),
    lambda p:p['documents'][1].update(derivedFrom='other'),
    lambda p:p['documents'][1].update(period='2026-09'),
    lambda p:p['documents'][1].update(url=REPORT),
    lambda p:p['documents'][0].update(url=DIRECTORY+'?page=2'),
    lambda p:p['limits'].update(maxHttpPerPeriod=3),
])
def test_graph_cannot_borrow_budget_or_bind_arbitrary_reports(mutation):
    p=discovery_policy();mutation(p)
    with pytest.raises(ValueError):import_module('app.macro.extensions.config').validate_group('householdCredit',p)


def runtime(monkeypatch,cap=2):
    from app.macro.extensions.config import load_extension_config, ExtensionConfig
    value=load_extension_config().model_dump();p=discovery_policy();p['collectionEnabled']=True
    p['limits']['maxHttpPerRun']=cap;value['groups']['householdCredit']=p
    config=ExtensionConfig.model_validate(value)
    module=import_module('app.macro.jobs.official_macro_runtime')
    monkeypatch.setattr(module,'load_extension_config',lambda:config)
    return module,config


@pytest.mark.parametrize('found',[False,True])
def test_durable_directory_report_runtime_and_zero_http_reentry(database,monkeypatch,found):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):
        calls.append(str(request.url));assert count(factory,JobRun)==len(calls)
        if len(calls)==1:return response(200,content=directory([(REPORT,'2026年前三季度金融统计数据报告')] if found else []))
        assert calls[-1]==REPORT
        return response(200,content=document())
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    again=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert first['status']==('completed' if found else 'source_pending')
    assert first['supplierHttp']==(2 if found else 1) and again['supplierHttp']==0
    assert count(factory,MacroObservation)==(1 if found else 0)
    if found:
        with factory() as s:
            rows=list(s.scalars(select(JobRun).order_by(JobRun.business_key)))
            parent=next(r for r in rows if r.details['document']['role']=='household-directory')
            child=next(r for r in rows if r.details['document']['role']=='household-ytd')
            assert child.details['request']['directoryRunId']==parent.id
            assert child.details['request']['directoryRawSha256']==parent.details['normalized']['rawSha256']
    view_module=import_module('app.macro.official_macro_view')
    view=view_module.load_official_macro_view(factory,NOW+timedelta(seconds=1))
    assert view_module.official_macro_view_valid(view)
    group=view['groups']['householdCredit']
    assert group['series'][0]['latest']==({'month':'2026-09-01','value':'12100'} if found else None)
    assert group['series'][1]['latest'] is None  # 缺同年相邻累计，不能把YTD当当月。
    assert group['latestAttempt']['status']==('completed' if found else 'source_pending')


def test_one_request_run_resumes_saved_directory_then_report(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch,cap=1);calls=[]
    def handler(request):
        calls.append(str(request.url))
        return response(200,content=directory([(REPORT,'2026年前三季度金融统计数据报告')]) if len(calls)==1 else document())
    values=[m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(3)]
    assert [(r['status'],r['supplierHttp']) for r in values]==[('partial',1),('completed',1),('completed',0)]


def test_no_candidate_new_week_can_check_again_but_never_fetches_old_report(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):calls.append(str(request.url));return response(200,content=directory([(REPORT,'2026年8月金融统计数据报告')]))
    for now in (NOW,NOW,NOW+timedelta(days=8)):
        r=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:now,transport=httpx.MockTransport(handler))
        assert r['status']=='source_pending'
    assert calls==[DIRECTORY,DIRECTORY] and count(factory,MacroObservation)==0


def test_report_period_mismatch_stays_received_and_replays_without_new_http(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):
        calls.append(str(request.url))
        return response(200,content=directory([(REPORT,'2026年前三季度金融统计数据报告')]) if len(calls)==1
            else document(title='2026年前八个月金融统计数据报告'))
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    again=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert first['status']==again['status']=='replay_pending' and first['supplierHttp']==2 and again['supplierHttp']==0
    assert len(calls)==2 and count(factory,MacroObservation)==0 and count(factory,SourcePayload)==2


@pytest.mark.parametrize('failed',[False,True])
def test_frozen_static_attempt_cannot_be_escaped_by_new_graph(database,monkeypatch,failed):
    _,factory=database
    store=import_module('app.macro.official_macro_store').OfficialMacroStore(factory)
    old=import_module('app.macro.official_macro_plan').build_official_macro_plan('householdCredit',policy(),NOW)
    key=store.reserve(old,old['documents'][0],NOW)
    if failed:store.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    m,c=runtime(monkeypatch)
    r=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW+timedelta(days=8),
        transport=httpx.MockTransport(lambda _:pytest.fail('old frozen failure must not send')))
    assert r['status']=='blocked' and r['supplierHttp']==0 and count(factory,JobRun)==1


@pytest.mark.parametrize('status',[403,302,429,500])
def test_directory_http_failure_never_requests_report_or_refunds_next_week(database,monkeypatch,status):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):calls.append(str(request.url));return response(status,content=b'failure')
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    later=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW+timedelta(days=8),transport=httpx.MockTransport(handler))
    assert first['status']==later['status']=='blocked' and first['supplierHttp']==1 and later['supplierHttp']==0
    assert calls==[DIRECTORY] and count(factory,SourcePayload)==count(factory,MacroObservation)==0


def test_saved_directory_tamper_blocks_before_report_send(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch,cap=1)
    first=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,
        transport=httpx.MockTransport(lambda _:response(200,content=directory([(REPORT,'2026年前三季度金融统计数据报告')]))))
    assert first['status']=='partial'
    with factory.begin() as s:
        raw=s.scalar(select(SourcePayload));value=deepcopy(raw.payload);value['bodyBase64']='eA=='
        raw.payload=value
    after=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,
        transport=httpx.MockTransport(lambda _:pytest.fail('tampered parent must not send')))
    assert after['status']=='blocked' and after['supplierHttp']==0 and count(factory,JobRun)==1


@pytest.mark.parametrize('duplicate_href',[False,True])
def test_ambiguous_saved_directory_replays_without_sending_either_report(database,monkeypatch,duplicate_href):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    other=REPORT.replace('2026101417073743604','2026101417073743605')
    def handler(request):
        calls.append(str(request.url))
        body=directory([(REPORT,'2026年9月金融统计数据报告'),(other,'2026年前三季度金融统计数据报告')])
        if duplicate_href:body=body.replace(('href="'+other+'"').encode(),('href="'+other+'" href="'+other+'"').encode())
        return response(200,content=body)
    reports=[m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(2)]
    assert [(r['status'],r['supplierHttp']) for r in reports]==[('replay_pending',1),('replay_pending',0)]
    assert calls==[DIRECTORY] and count(factory,SourcePayload)==1 and count(factory,MacroObservation)==0


def test_disable_between_directory_and_report_preserves_only_completed_parent(database,monkeypatch):
    from app.macro.extensions.config import ExtensionConfig
    _,factory=database;m,c=runtime(monkeypatch);waits=[];calls=[]
    def before_send():
        waits.append(True)
        if len(waits)==2:
            value=c.model_dump();value['groups']['householdCredit']['collectionEnabled']=False
            monkeypatch.setattr(m,'load_extension_config',lambda:ExtensionConfig.model_validate(value))
    def handler(request):calls.append(str(request.url));return response(200,content=directory([(REPORT,'2026年前三季度金融统计数据报告')]))
    result=m.execute_official_macro_group(factory,c,'householdCredit',clock=lambda:NOW,
        transport=httpx.MockTransport(handler),before_send=before_send)
    assert result['status']=='blocked' and result['supplierHttp']==1 and result['errorCode']=='EXTENSION_COLLECTION_CHANGED'
    assert calls==[DIRECTORY] and count(factory,JobRun)==count(factory,SourcePayload)==1


def complete_graph(factory,now,amount,lookback=None):
    policy_value=discovery_policy()
    if lookback is not None:policy_value['lookbackPeriods']=lookback
    p=plan(policy_value,now=now);month=p['endPeriod'][:7]
    source=policy()['documents'][0]['url'] if month=='2026-08' else REPORT
    title=f'{month[:4]}年前{int(month[5:])}个月金融统计数据报告'
    store=import_module('app.macro.official_macro_store').OfficialMacroStore(factory)
    parent=store.reserve(p,p['documents'][0],now)
    store.receive(parent,directory([(source,title)]),now+timedelta(seconds=1));store.replay(parent,now+timedelta(seconds=2))
    child=store.reserve(p,p['documents'][1],now+timedelta(seconds=3))
    store.receive(child,document(title=title,amount=amount),now+timedelta(seconds=4));store.replay(child,now+timedelta(seconds=5))
    return p


def history_view(factory,now=NOW):
    return import_module('app.macro.official_macro_view').load_official_macro_view(factory,now+timedelta(seconds=10))


def test_previous_completed_month_survives_new_root_and_monthly_delta_is_computed(database):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=28),'1.00');complete_graph(factory,NOW,'1.21')
    value=history_view(factory);group=value['groups']['householdCredit']
    assert len(group['history'])==1 and 'history' not in group['history'][0]
    assert group['series'][0]['points'][-2:]==[{'month':'2026-08-01','value':'10000'},{'month':'2026-09-01','value':'12100'}]
    assert group['series'][1]['latest']=={'month':'2026-09-01','value':'2100'}
    assert import_module('app.macro.official_macro_view').official_macro_view_valid(value)


def test_repeated_month_uses_latest_visible_revision_without_mutating_old_originals(database):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=28),'1.00');complete_graph(factory,NOW,'1.21')
    with factory() as s:before={r.id:(r.content_hash,deepcopy(r.payload)) for r in s.scalars(select(SourcePayload))}
    complete_graph(factory,NOW+timedelta(days=1),'1.30')
    group=history_view(factory,NOW+timedelta(days=1))['groups']['householdCredit']
    assert group['series'][1]['latest']=={'month':'2026-09-01','value':'3000'}
    assert len(group['history'])==1
    with factory() as s:
        assert all((s.get(SourcePayload,k).content_hash,s.get(SourcePayload,k).payload)==v for k,v in before.items())


def test_new_week_failure_keeps_completed_history_and_does_not_invent_missing_month(database):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=28),'1.00');complete_graph(factory,NOW,'1.21')
    before=history_view(factory)['groups']['householdCredit']
    later=NOW+timedelta(days=1);p=plan(now=later);store=import_module('app.macro.official_macro_store').OfficialMacroStore(factory)
    key=store.reserve(p,p['documents'][0],later);store.fail(key,'EXTENSION_AUTH_ERROR',later+timedelta(seconds=1))
    after=history_view(factory,later)['groups']['householdCredit']
    assert after['series']==before['series'] and after['history']==before['history']
    assert after['latestAttempt']['status']=='failed'
    assert after['series'][1]['points'][-2]['value'] is None


def test_history_does_not_spread_missing_months_or_subtract_across_years(database):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=28),'1.00')
    complete_graph(factory,NOW+timedelta(days=28),'1.40')
    group=history_view(factory,NOW+timedelta(days=28))['groups']['householdCredit']
    assert group['series'][1]['latest'] is None
    december=datetime(2027,1,24,6,tzinfo=NOW.tzinfo);january=datetime(2027,2,21,6,tzinfo=NOW.tzinfo)
    complete_graph(factory,december,'2.00');complete_graph(factory,january,'0.50')
    group=history_view(factory,january)['groups']['householdCredit']
    assert group['series'][1]['latest']=={'month':'2027-01-01','value':'5000'}


def test_window_keeps_one_prior_month_baseline_but_not_unneeded_older_roots(database):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=56),'0.70')
    complete_graph(factory,NOW-timedelta(days=28),'1.00')
    complete_graph(factory,NOW,'1.21')
    current=NOW+timedelta(days=28);complete_graph(factory,current,'1.40',lookback=2)
    value=history_view(factory,current);group=value['groups']['householdCredit']
    assert len(group['history'])==2
    assert group['series'][1]['points']==[{'month':'2026-09-01','value':'2100'},{'month':'2026-10-01','value':'1900'}]
    group['history'].reverse()
    from app.macro.extensions.plan import digest
    value['evidenceHash']=digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not import_module('app.macro.official_macro_view').official_macro_view_valid(value)


@pytest.mark.parametrize('mutation',['amount','duplicate','nested','future','missing-doc'])
def test_history_group_cannot_be_forged_reordered_or_nested(database,mutation):
    _,factory=database
    complete_graph(factory,NOW-timedelta(days=28),'1.00');complete_graph(factory,NOW,'1.21')
    value=history_view(factory);group=value['groups']['householdCredit'];old=group['history'][0]
    if mutation=='amount':old['documents'][1]['normalized']['points'][0]['value']='999'
    elif mutation=='duplicate':group['history'].append(deepcopy(old))
    elif mutation=='nested':old['history']=[]
    elif mutation=='future':old['plan']=deepcopy(group['plan'])
    else:old['documents'].pop()
    from app.macro.extensions.plan import digest
    value['evidenceHash']=digest({k:v for k,v in value.items() if k!='evidenceHash'})
    assert not import_module('app.macro.official_macro_view').official_macro_view_valid(value)
