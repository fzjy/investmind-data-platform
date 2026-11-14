"""一线房价历史计划：真实捕获时间，目录原件证明，不伪造过去周。"""
from datetime import date,datetime,timezone
from hashlib import sha256
import re
from urllib.parse import urljoin

from pydantic import Field,model_validator
from app.macro.extensions.policy_types import StrictModel
from app.macro.official_macro_normalize import _decode,_document,_month
from app.macro.tier1_housing_discovery import ROOT,REPORT,_Listing

DIRECTORY_VERSION='tier1-housing-history-directory-v1'
REPORT_VERSION='tier1-housing-history-report-v1'
VERSIONS=(DIRECTORY_VERSION,REPORT_VERSION)


def digest(value):
    from app.macro.extensions.plan import digest as canonical_digest
    return canonical_digest(value)


class HistoryPolicy(StrictModel):
    collectionEnabled: bool
    maxDirectoryPages: int=Field(ge=1,le=50)
    maxHttpPerRun: int=Field(ge=1,le=3)
    maxHttpTotal: int=Field(ge=2,le=98)
    requestsPerMinute: int=Field(ge=1,le=10)
    requestTimeoutSeconds: int=Field(ge=1,le=20)
    maxRetries: int=Field(ge=0,le=0)
    maxResponseBytes: int=Field(ge=1,le=5242880)
    maxResponseRows: int=Field(ge=2,le=16)

    @model_validator(mode='after')
    def bounded(self):
        if self.maxHttpTotal<=self.maxDirectoryPages:raise ValueError('invalid total budget')
        return self


def page_url(page):
    if type(page) is not int or not 0<=page<50:raise ValueError('HISTORY_PAGE_INVALID')
    return ROOT if page==0 else ROOT+f'index_{page}.html'


def _index(month):
    value=_month(month[:7]);return value.year*12+value.month


def build_directory_plan(policy,now,start,end,known):
    checked=HistoryPolicy.model_validate(policy);frozen=checked.model_dump(exclude={'collectionEnabled'})
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('HISTORY_CLOCK_INVALID')
    now=now.astimezone(timezone.utc)
    for period in (start,end):
        if type(period) is not str or _month(period[:7]).isoformat()!=period:raise ValueError('HISTORY_PERIOD_INVALID')
    width=_index(end)-_index(start)+1
    if not 1<=width<=48 or checked.maxHttpTotal<checked.maxDirectoryPages+width:raise ValueError('HISTORY_TOTAL_BUDGET_INVALID')
    if type(known) is not list or known!=sorted(set(known)) or any(type(p) is not str or not start<=_month(p).isoformat()<=end for p in known):raise ValueError('HISTORY_KNOWN_PERIOD_INVALID')
    if end>=now.date().replace(day=1).isoformat():raise ValueError('HISTORY_FUTURE_PERIOD')
    docs=[dict(id=f'history-page-{i}',role='tier1-history-directory',period=f'page-{i}',url=page_url(i),derivedFrom=None) for i in range(checked.maxDirectoryPages)]
    limits={k:frozen[k] for k in ('requestsPerMinute','requestTimeoutSeconds','maxRetries','maxResponseBytes','maxResponseRows')}
    limits.update(maxHttpPerRun=min(checked.maxHttpPerRun,len(docs)),maxHttpPerPeriod=len(docs),maxDirectoryCandidates=2)
    value=dict(schemaVersion=1,normalizerVersion=DIRECTORY_VERSION,groupId='tier1Housing',groupPolicy=frozen,
        groupPolicyHash=digest(frozen),scheduledAt=now.isoformat(),startPeriod=start,endPeriod=end,latestQuarter='not-applicable',
        documents=docs,limits=limits,knownPeriods=known)
    return {**value,'fingerprint':digest(value)}


def parse_history_directory(body,plan,page):
    try:
        _,text=_document(body)
        if '数据解读' not in text:raise ValueError()
        p=_Listing();p.feed(_decode(body));p.close()
        if p.stack or p.script is not None or not p.found or p.rows!=10 or p.links.href is not None:raise ValueError()
        scripts='\n'.join(s for s in p.scripts if s is not None)
        for key,value in [('m_nCurrPage',page),('m_nPageSize',10)]:
            if re.findall(r'\bvar\s+'+key+r'\s*=\s*([0-9]+)\s*;',scripts)!=[str(value)]:raise ValueError()
        counts=re.findall(r'\bvar\s+m_nRecordCount\s*=\s*"([0-9]+)"\s*;',scripts)
        if len(counts)!=1 or not plan['groupPolicy']['maxDirectoryPages']*10<=int(counts[0])<=100000:raise ValueError()
        candidates={}
        for href,label in p.links.links:
            match=re.fullmatch(r'(?:国家统计局城市司[^年]{0,40}解读)?([0-9]{4})年([0-9]{1,2})月份商品住宅销售价格变动情况(?:统计数据|解读)?',re.sub(r'\s+','',label))
            if match is None:continue
            period=date(int(match[1]),int(match[2]),1).isoformat()
            if not plan['startPeriod']<=period<=plan['endPeriod']:continue
            if href!=href.strip() or any(ord(c)<33 for c in href) or any(c in href for c in ('..','%','\\')):raise ValueError()
            url=urljoin(ROOT,href)
            if re.fullmatch(REPORT,url) is None or (period in candidates and candidates[period]!=url):raise ValueError()
            candidates[period]=url
        return dict(schemaVersion='1.0',role='tier1-history-directory',source='nbs',rawSha256=sha256(body).hexdigest(),
            page=page,recordCount=int(counts[0]),candidates=[dict(period=m[:7],url=u) for m,u in sorted(candidates.items())])
    except Exception:raise ValueError('HISTORY_DIRECTORY_INVALID') from None


def _directory_proofs(plan,documents):
    from app.macro.official_macro_store import run_key
    if type(documents) is not list or len(documents)!=len(plan['documents']):raise ValueError('HISTORY_DIRECTORY_INCOMPLETE')
    catalog={};previous=datetime.fromisoformat(plan['scheduledAt']);counts=set()
    for i,(doc,item) in enumerate(zip(plan['documents'],documents,strict=True)):
        if type(item) is not dict or set(item)!={'runId','documentId','request','retrievedAt','completedAt','normalized',*(['recovery'] if 'recovery' in item else [])}:raise ValueError()
        if 'recovery' in item:
            from app.macro.housing_history_recovery import validate_public_recovery
            validate_public_recovery(item['recovery'],run_key(plan,doc),item['runId'],item['retrievedAt'],item['completedAt'],plan,doc)
        elif item['runId']!=run_key(plan,doc):raise ValueError()
        if (item['documentId']!=doc['id'] or item['request']!=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)):raise ValueError()
        retrieved=datetime.fromisoformat(item['retrievedAt']);completed=datetime.fromisoformat(item['completedAt'])
        if retrieved.tzinfo is None or retrieved.isoformat()!=item['retrievedAt'] or completed.isoformat()!=item['completedAt'] or retrieved<previous or completed<retrieved:raise ValueError()
        previous=completed;n=item['normalized']
        if (type(n) is not dict or set(n)!={'schemaVersion','role','source','rawSha256','page','recordCount','candidates'}
            or n['schemaVersion']!='1.0' or n['role']!='tier1-history-directory' or n['source']!='nbs'
            or type(n['page']) is not int or n['page']!=i or type(n['rawSha256']) is not str or re.fullmatch('[0-9a-f]{64}',n['rawSha256']) is None
            or type(n['recordCount']) is not int or not len(documents)*10<=n['recordCount']<=100000
            or type(n['candidates']) is not list or len(n['candidates'])>10):raise ValueError()
        counts.add(n['recordCount']);months=[]
        for c in n['candidates']:
            if type(c) is not dict or set(c)!={'period','url'} or type(c['url']) is not str or re.fullmatch(REPORT,c['url']) is None:raise ValueError()
            m=_month(c['period']).isoformat()
            if not plan['startPeriod']<=m<=plan['endPeriod'] or (m in catalog and catalog[m]!=c['url']):raise ValueError('HISTORY_REPORT_AMBIGUOUS')
            months.append(m);catalog[m]=c['url']
        if months!=sorted(set(months)):raise ValueError()
    if len(counts)!=1:raise ValueError('HISTORY_DIRECTORY_CHANGED')
    return catalog


def build_report_plan(directory_plan,documents):
    validate_history_plan(directory_plan)
    catalog=_directory_proofs(directory_plan,documents)
    docs=[dict(id='history-'+m[:7].replace('-',''),role='tier1-prices',period=m[:7],url=u,derivedFrom=None)
        for m,u in sorted(catalog.items()) if m[:7] not in directory_plan['knownPeriods']]
    if not docs:return None
    if len(docs)+len(documents)+sum('recovery' in d for d in documents)>directory_plan['groupPolicy']['maxHttpTotal']:raise ValueError('HISTORY_TOTAL_BUDGET_INVALID')
    value={k:v for k,v in directory_plan.items() if k not in ('fingerprint','documents','limits','normalizerVersion')}
    value.update(normalizerVersion=REPORT_VERSION,documents=docs,limits={**directory_plan['limits'],
        'maxHttpPerRun':min(directory_plan['groupPolicy']['maxHttpPerRun'],len(docs)),'maxHttpPerPeriod':len(docs)},
        directoryPlan=directory_plan,directoryDocuments=documents)
    return {**value,'fingerprint':digest(value)}


def validate_history_plan(plan):
    try:
        if type(plan) is not dict:raise ValueError()
        base=build_directory_plan({**plan['groupPolicy'],'collectionEnabled':False},datetime.fromisoformat(plan['scheduledAt']),
            plan['startPeriod'],plan['endPeriod'],plan['knownPeriods'])
        if plan['normalizerVersion']==REPORT_VERSION:
            if plan.get('directoryPlan')!=base:raise ValueError()
            expected=build_report_plan(base,plan['directoryDocuments'])
        elif plan['normalizerVersion']==DIRECTORY_VERSION:expected=base
        else:raise ValueError()
        if expected!=plan:raise ValueError()
    except Exception:raise ValueError('HISTORY_PLAN_INVALID') from None
