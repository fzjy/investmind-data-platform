"""一线房价仅从受控目录提取目标月；无真实网络。"""
from copy import deepcopy
from datetime import timedelta
from html import escape
from importlib import import_module

import httpx
import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_tier1_housing import document

ROOT='https://www.stats.gov.cn/xxgk/jd/sjjd2020/'
PAGE=ROOT+'index_1.html'
REPORT=policy('tier1Housing')['documents'][0]['url']
LABEL='国家统计局城市司高级统计师杨彩芳解读2026年9月份商品住宅销售价格变动情况统计数据'


def directory(links=(),page=0):
    # 与官网相同：标题Document、可见栏名及10条列表、声明式分页数据。
    entries=list(links)+[('./202610/t20261019_1965630.html','其他数据解读')]*(10-len(links))
    return ('<html><title>Document</title><h2>数据解读</h2><script>'
        f'var m_nRecordCount = "500";var m_nCurrPage = {page};var m_nPageSize = 10;'
        '</script><ul class="list">'+''.join('<li class="list-item"><a href="'+escape(url,quote=True)+'">'+escape(label)+'</a></li>'
        for url,label in entries)+'</ul></html>').encode()


def parse(body=None,page=0,month='2026-09'):
    try:m=import_module('app.macro.tier1_housing_discovery')
    except ModuleNotFoundError:pytest.fail('tier1 housing directory discovery not implemented')
    return m.parse_tier1_directory(directory([(REPORT,LABEL)],page) if body is None else body,month,page=page,max_candidates=2)


def discovery_policy():
    p=policy('tier1Housing')
    p['limits'].update(maxHttpPerRun=3,maxHttpPerPeriod=3)
    p['documents']=[dict(id='housing-directory',role='tier1-directory',period='latest',url=ROOT,derivedFrom=None),
        dict(id='housing-page',role='tier1-directory-page',period='latest',url=PAGE,derivedFrom=None),
        dict(id='housing-report',role='tier1-prices',period='latest-completed-month',url=None,derivedFrom='housing-page')]
    return p


def plan(now=NOW):
    return import_module('app.macro.official_macro_plan').build_official_macro_plan('tier1Housing',discovery_policy(),now)


def test_target_month_from_label_not_publication_url_and_two_housing_types_remain_separate():
    n=parse()
    assert n['selected']==dict(period='2026-09',url=REPORT)
    assert n['role']=='tier1-directory' and n['page']==0 and n['source']=='nbs' and n['candidateCount']==1
    assert parse(directory([(REPORT,LABEL)],1),page=1)['role']=='tier1-directory-page'
    assert parse(directory([(REPORT,LABEL)]),month='2026-10')['selected'] is None


@pytest.mark.parametrize('url',['https://evil.example/r',REPORT+'?x=1',REPORT+'#a',REPORT.replace('202610','../202610'),
    REPORT.replace('www.stats.gov.cn','www.stats.gov.cn.evil.example'),' '+REPORT,REPORT.replace('202610','%32202610'),
    REPORT.replace('https://','http://')])
def test_bad_target_link_is_error_not_absence(url):
    with pytest.raises(ValueError):parse(directory([(url,LABEL)]))


@pytest.mark.parametrize('body',[
    b'<title>Document</title>',b'<title>Access denied</title>',directory(page=1),
    directory().replace(b'm_nCurrPage',b'unknown'),directory().replace(b'500',b'5'),
    directory().replace(b'class="list-item"',b'class="other"',1),
    directory().replace(b'</ul>',b''),directory().replace(b'm_nPageSize = 10',b'm_nPageSize = 20'),
    directory().replace(b'var m_nCurrPage = 0;',b'var m_nCurrPage = 0;var m_nCurrPage = 1;'),
])
def test_wrong_or_incomplete_directory_cannot_complete_check(body):
    with pytest.raises(ValueError):parse(body)


def test_two_pages_select_root_or_page_candidate_dedupe_same_url_and_reject_cross_page_revision():
    parse();select_report=import_module('app.macro.tier1_housing_discovery').select_tier1_report
    empty0=parse(directory(),page=0);empty1=parse(directory(page=1),page=1)
    found0=parse();found1=parse(page=1)
    assert select_report([empty0,empty1]) is None
    for pages in ([found0,empty1],[empty0,found1],[found0,found1]):
        assert select_report(pages)==dict(period='2026-09',url=REPORT)
    other=deepcopy(found1);other['selected']['url']=REPORT.replace('1965610','1965611')
    for pages in ([found0,other],[found1,found0],[found0],[]):
        with pytest.raises(ValueError):select_report(pages)


@pytest.mark.parametrize('mutation',[
    lambda p:p['documents'].reverse(),lambda p:p['documents'][2].update(derivedFrom='housing-directory'),
    lambda p:p['documents'][1].update(url=ROOT+'index_2.html'),lambda p:p['limits'].update(maxHttpPerPeriod=4),
    lambda p:p['documents'][2].update(period='2026-09'),
])
def test_graph_cannot_expand_pages_or_misbind_report(mutation):
    p=discovery_policy();mutation(p)
    with pytest.raises(ValueError):import_module('app.macro.extensions.config').validate_group('tier1Housing',p)


def test_old_static_contract_cannot_be_turned_into_an_unbound_derived_report():
    p=policy('tier1Housing')
    p['documents'][0].update(url=None,period='latest-completed-month',derivedFrom='missing-page')
    with pytest.raises(ValueError):import_module('app.macro.extensions.config').validate_group('tier1Housing',p)


def runtime(monkeypatch,cap=3):
    from app.macro.extensions.config import load_extension_config, ExtensionConfig
    value=load_extension_config().model_dump();p=discovery_policy();p['collectionEnabled']=True;p['limits']['maxHttpPerRun']=cap
    value['groups']['tier1Housing']=p;c=ExtensionConfig.model_validate(value)
    m=import_module('app.macro.jobs.official_macro_runtime');monkeypatch.setattr(m,'load_extension_config',lambda:c)
    return m,c


@pytest.mark.parametrize('found',[None,0,1])
def test_durable_two_pages_then_unique_report_or_absence_and_zero_http_reentry(database,monkeypatch,found):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url));assert count(factory,JobRun)==len(calls)
        assert calls[-1]==[ROOT,PAGE,REPORT][i]
        return response(200,content=directory([(REPORT,LABEL)] if found==i else [],page=i) if i<2 else document())
    values=[m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(2)]
    assert [(v['status'],v['supplierHttp']) for v in values]==([('source_pending',2),('source_pending',0)] if found is None else [('completed',3),('completed',0)])
    assert count(factory,MacroObservation)==(0 if found is None else 2)
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,NOW+timedelta(seconds=1))
    g=view['groups']['tier1Housing']
    assert g['latestAttempt']['status']==('source_pending' if found is None else 'completed')
    assert g['series'][0]['latest']==(None if found is None else dict(month='2026-09-01',value='-0.4'))


def test_one_request_per_run_is_resumable_without_repeating_directories(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch,1);calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url));return response(200,content=directory([(REPORT,LABEL)],page=i) if i<2 else document())
    values=[m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(handler)) for _ in range(4)]
    assert [(v['status'],v['supplierHttp']) for v in values]==[('partial',1),('partial',1),('completed',1),('completed',0)]
    assert calls==[ROOT,PAGE,REPORT]


def test_cross_page_conflict_never_fetches_report_or_writes_core(database,monkeypatch):
    _,factory=database;m,c=runtime(monkeypatch);calls=[]
    def handler(request):
        i=len(calls);calls.append(str(request.url));assert i<2
        return response(200,content=directory([(REPORT if i==0 else REPORT.replace('1965610','1965611'),LABEL)],page=i))
    first=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert first['status']=='replay_pending' and first['supplierHttp']==2 and count(factory,MacroObservation)==0
    assert m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(handler))['supplierHttp']==0
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,NOW+timedelta(seconds=1))
    assert view['groups']['tier1Housing']['latestAttempt']['status']=='pending'


@pytest.mark.parametrize('key',['page0','page1','missing'])
@pytest.mark.parametrize('change',['order','raw','false-complete','false-absence','early-page'])
def test_python_public_view_rejects_forged_completion_and_parent_evidence(key,change):
    from backend.tests.e2e.export_controlled_tier1_housing_discovery import cases
    from app.macro.official_macro_view import official_macro_view_valid
    from app.macro.extensions.plan import digest
    v=deepcopy(cases()[key]);g=v['groups']['tier1Housing'];docs=g['documents']
    if change=='order':docs.reverse()
    elif change=='raw':docs[1]['request']['directoryRawSha256']='0'*64
    elif change=='false-complete':g['documents']=docs[:1];g['latestAttempt']['status']='completed'
    elif change=='false-absence':g['documents']=docs[:1];g['latestAttempt']['status']='source_pending'
    else:docs[1]['retrievedAt']=docs[0]['retrievedAt']
    v['evidenceHash']=digest({k:x for k,x in v.items() if k!='evidenceHash'})
    assert not official_macro_view_valid(v)


@pytest.mark.parametrize('legacy',[True,False])
def test_same_cycle_terminal_legacy_is_retained_and_next_week_absence_does_not_erase_real_history(database,monkeypatch,legacy):
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    _,factory=database;value=load_extension_config().model_dump();old=policy('tier1Housing') if legacy else discovery_policy();old['collectionEnabled']=True
    value['groups']['tier1Housing']=old;c=ExtensionConfig.model_validate(value)
    m=import_module('app.macro.jobs.official_macro_runtime');monkeypatch.setattr(m,'load_extension_config',lambda:c)
    initial=[]
    def first_handler(request):
        i=len(initial);initial.append(str(request.url));return response(200,content=document() if legacy or i==2 else directory([(REPORT,LABEL)],i))
    r=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(first_handler))
    assert r['status']=='completed' and r['supplierHttp']==(1 if legacy else 3)
    m,c=runtime(monkeypatch);calls=[]
    def handler(request):
        calls.append(str(request.url));return response(200,content=directory(page=len(calls)-1))
    r=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(handler))
    assert r['status']=='completed' and r['supplierHttp']==0 and r['readOnly']
    next_week=NOW+timedelta(days=8)
    r=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:next_week,transport=httpx.MockTransport(handler))
    assert r['status']=='source_pending' and r['supplierHttp']==2
    view=import_module('app.macro.official_macro_view').load_official_macro_view(factory,next_week+timedelta(seconds=1))
    g=view['groups']['tier1Housing']
    assert g['series'][0]['latest']==dict(month='2026-09-01',value='-0.4')
    assert g['latestAttempt']['status']=='source_pending'
    assert g['plan']['endPeriod']=='2026-10-01' and g['plan']==g['latestAttempt']['plan']
    assert [d['documentId'] for d in g['documents']]==['housing-directory','housing-page']
    assert len(g['history'])==1 and g['history'][0]['plan']['endPeriod']=='2026-09-01'
    assert g['series'][0]['points'][-1]==dict(month='2026-10-01',value=None)
    assert '2026-10-01' in g['series'][0]['missingPeriods']


def test_failed_legacy_cycle_is_not_bypassed_by_new_configuration(database,monkeypatch):
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    _,factory=database;value=load_extension_config().model_dump();old=policy('tier1Housing');old['collectionEnabled']=True
    value['groups']['tier1Housing']=old;c=ExtensionConfig.model_validate(value)
    m=import_module('app.macro.jobs.official_macro_runtime');monkeypatch.setattr(m,'load_extension_config',lambda:c)
    r=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW,transport=httpx.MockTransport(lambda _:response(403)))
    assert r['status']=='blocked'
    m,c=runtime(monkeypatch)
    r=m.execute_official_macro_group(factory,c,'tier1Housing',clock=lambda:NOW+timedelta(days=8),transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert r['status']=='blocked' and r['supplierHttp']==0
