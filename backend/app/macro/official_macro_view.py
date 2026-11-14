"""有原件/Core绑定的具名宏观视图；累计拆月、季度不填充、修订不覆盖。"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.ops import JobRun
from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import canonical, digest
from app.macro.extensions.store import FAILURE_CODES, instant
from app.macro.official_macro_normalize import _decimal, _decimal_text, _quarter, derive_household_months, derive_special_progress
from app.macro.official_macro_store import JOB, OfficialMacroStore, run_key, validate_plan

BASIS = 'normalization-visible-restated-history-not-strict-pit'
SHAPES = {
    'household-ytd': [('cn.credit.household_medium_long_term_ytd','人民币住户中长期贷款年初累计净增','亿元','monthly')],
    'tier1-prices': [('cn.house.price_70_city_mom.tier1','一线城市新建商品住宅销售价格环比','%','monthly'),
                     ('cn.house.price_70_city_mom.tier1.existing','一线城市二手住宅销售价格环比','%','monthly')],
    'special-quota': [('cn.fiscal.special_bonds_new_quota','年度地方政府新增专项债务原限额','亿元','annual')],
    'special-issuance': [('cn.fiscal.special_bonds_new_month','地方政府新增专项债券当月发行额（直报）','亿元','monthly'),
                         ('cn.fiscal.special_bonds_new_ytd','地方政府新增专项债券年初累计发行额','亿元','monthly')],
    'property-report': [('cn.credit.property_loans_balance','人民币房地产贷款余额','亿元','quarterly'),
                        ('cn.credit.property_loans_yoy','人民币房地产贷款余额同比','%','quarterly')],
}
SHAPES['special-briefing'] = [*SHAPES['special-issuance'],
    ('cn.fiscal.special_bonds_reported_progress','新增专项债券发行进度（发布会直报）','%','monthly')]
SOURCE = {'household-ytd':'pboc','tier1-prices':'nbs','tier1-directory':'nbs','tier1-directory-page':'nbs','special-quota':'mof','special-directory':'mof','special-budget-directory':'mof','special-year-directory':'mof',
          'special-issuance':'mof','special-briefing':'mof','property-report':'pboc','property-directory':'pboc','household-directory':'pboc'}
SERIES = {
    'householdCredit': [*SHAPES['household-ytd'],
        ('cn.credit.household_medium_long_term_month','人民币住户中长期贷款当月净增（同年相邻累计差）','亿元','monthly')],
    'tier1Housing': SHAPES['tier1-prices'],
    'specialBonds': SHAPES['special-briefing'],
    'propertyLoans': SHAPES['property-report'],
}


def _timestamp(value, ceiling):
    if type(value) is not str:
        raise ValueError()
    result = instant(datetime.fromisoformat(value))
    if result.isoformat() != value or not value.endswith('+00:00') or result > ceiling:
        raise ValueError()
    return result


def _number(value):
    result = _decimal(value)
    if _decimal_text(result) != value:
        raise ValueError()
    return result


def _normalized(v, doc, request, plan):
    role = doc['role']
    if type(v) is not dict or v.get('schemaVersion') != '1.0' or v.get('role') != role or v.get('source') != SOURCE[role]:
        raise ValueError()
    if type(v.get('rawSha256')) is not str or re.fullmatch('[0-9a-f]{64}',v['rawSha256']) is None:
        raise ValueError()
    if role in ('tier1-directory','tier1-directory-page'):
        from app.macro.tier1_housing_discovery import validate_tier1_directory
        validate_tier1_directory(v,plan['endPeriod'][:7],0 if role=='tier1-directory' else 1)
        return
    if role in ('special-budget-directory','special-year-directory'):
        from app.macro.special_quota_discovery import quota_link
        year=plan['endPeriod'][:4]
        if (set(v)!={'schemaVersion','role','source','rawSha256','targetYear','candidateCount','selected'} or v['targetYear']!=year
            or type(v['candidateCount']) is not int or v['candidateCount'] not in (0,1) or (v['candidateCount']==0)!=(v['selected'] is None)):raise ValueError()
        if v['selected'] is not None:
            selected=v['selected']
            if type(selected) is not dict or set(selected)!={'year','url'} or selected['year']!=year or not quota_link(role,year,selected['url']):raise ValueError()
        return
    if role in ('household-directory','special-directory'):
        if (set(v) != {'schemaVersion','role','source','rawSha256','targetMonth','candidateCount','selected'}
                or v['targetMonth'] != plan['endPeriod'][:7] or type(v['candidateCount']) is not int
                or v['candidateCount'] not in (0,1) or (v['candidateCount']==0)!=(v['selected'] is None)):
            raise ValueError()
        if v['selected'] is not None:
            selected=v['selected']
            if (type(selected) is not dict or set(selected)!={'period','url'} or selected['period']!=v['targetMonth']
                    or re.fullmatch(r'https://www\.pbc\.gov\.cn/diaochatongjisi/116219/116225/[0-9]{17,22}/index\.html' if role=='household-directory' else r'https://zwgls\.mof\.gov\.cn/tjsj/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm',selected['url']) is None):
                raise ValueError()
        return
    if role == 'property-directory':
        if set(v) != {'schemaVersion','role','source','rawSha256','latestQuarter','candidateCount','selected'} or v['latestQuarter'] != plan['latestQuarter']:
            raise ValueError()
        count = v['candidateCount']
        if type(count) is not int or not 0 <= count <= plan['limits']['maxDirectoryCandidates'] or (count == 0) != (v['selected'] is None):
            raise ValueError()
        selected = v['selected']
        if selected is not None:
            if type(selected) is not dict or set(selected) != {'quarter','url'} or re.fullmatch(r'https://www\.pbc\.gov\.cn/diaochatongjisi/116219/116225/[0-9]{17,22}/index\.html',selected['url']) is None:
                raise ValueError()
            y,q,_ = _quarter(plan['latestQuarter']);sy,sq,_ = _quarter(selected['quarter'])
            if not 0 <= y*4+q-sy*4-sq <= plan['groupPolicy']['quarterFallbacks']:
                raise ValueError()
        return
    if set(v) != {'schemaVersion','role','period','source','rawSha256','points','events'}:
        raise ValueError()
    period = (_quarter(request['period'])[2].isoformat() if role == 'property-report' else
              request['period']+'-01-01' if role == 'special-quota' else request['period']+'-01')
    if v['period'] != period or type(v['points']) is not list or len(v['points']) != len(SHAPES[role]):
        raise ValueError()
    for i,(point,shape) in enumerate(zip(v['points'],SHAPES[role],strict=True)):
        sid,name,unit,frequency = shape
        expected = {'seriesId':sid,'period':period,'name':name,'unit':unit,'frequency':frequency}
        if role == 'tier1-prices':
            expected['housingType'] = ('new','existing')[i]
        if type(point) is not dict or set(point) != {*expected,'value'} or any(point[k] != value for k,value in expected.items()):
            raise ValueError()
        number = _number(point['value'])
        if ((role.startswith('special-') or sid.endswith('_balance')) and number < 0) or (role == 'special-quota' and number <= 0):
            raise ValueError()
        if (role == 'tier1-prices' or sid.endswith('_yoy')) and number < -100:
            raise ValueError()
    if type(v['events']) is not list or len(v['events']) > (1 if role == 'special-briefing' else 0):
        raise ValueError()
    for event in v['events']:
        if (type(event) is not dict or set(event) != {'kind','amount','unit','effectivePeriod'}
            or event['kind'] != 'special-carryover-allocation' or event['unit'] != '亿元' or event['effectivePeriod'] is not None
            or _number(event['amount']) < 0):
            raise ValueError()


def _documents(group, group_id, ceiling):
    if type(group) is not dict or set(group) != {'plan','documents','latestAttempt'} or type(group['documents']) is not list:
        raise ValueError()
    plan = group['plan']
    if plan is None:
        if group['documents'] or group['latestAttempt'] is not None:
            raise ValueError()
        return
    validate_plan(plan)
    if plan['groupId'] != group_id or instant(datetime.fromisoformat(plan['scheduledAt'])) > ceiling:
        raise ValueError()
    if len(group['documents']) > len(plan['documents']):
        raise ValueError()
    special_discovery=group_id=='specialBonds' and plan['documents'][0]['role'] in ('special-directory','special-budget-directory')
    housing_discovery=group_id=='tier1Housing' and plan['documents'][0]['role']=='tier1-directory'
    from app.macro.housing_history_plan import REPORT_VERSION
    archive=plan['normalizerVersion']==REPORT_VERSION
    if (special_discovery or housing_discovery or archive) and any(item['documentId']!=plan['documents'][i]['id'] for i,item in enumerate(group['documents'])):
        raise ValueError('OFFICIAL_SPECIAL_DOCUMENT_ORDER_INVALID')
    seen = {}
    for item in group['documents']:
        if (archive or housing_discovery or (special_discovery and plan['documents'][0]['role']=='special-budget-directory')) and seen:
            if _timestamp(item['retrievedAt'],ceiling)<_timestamp(next(reversed(seen.values()))['completedAt'],ceiling):raise ValueError('OFFICIAL_SPECIAL_SEQUENCE_TIME_INVALID')
        if type(item) is not dict or set(item) != {'runId','documentId','request','retrievedAt','completedAt','normalized',*(['recovery'] if 'recovery' in item else [])}:
            raise ValueError()
        doc = next(d for d in plan['documents'] if d['id'] == item['documentId'])
        if item['documentId'] in seen:
            raise ValueError()
        if 'recovery' in item:
            _recovery_document(item,doc,plan,ceiling)
        elif item['runId']!=run_key(plan,doc):raise ValueError()
        if (_timestamp(item['completedAt'],ceiling) < _timestamp(item['retrievedAt'],ceiling)
            or _timestamp(item['retrievedAt'],ceiling) < instant(datetime.fromisoformat(plan['scheduledAt']))):
            raise ValueError()
        if archive and _timestamp(item['retrievedAt'],ceiling)<_timestamp(plan['directoryDocuments'][-1]['completedAt'],ceiling):raise ValueError('HISTORY_REPORT_BEFORE_DIRECTORY')
        request = {'url':doc['url'],'period':doc['period'],'directoryRunId':None,'directoryRawSha256':None}
        if doc['derivedFrom'] is not None:
            parent = seen[doc['derivedFrom']]
            if special_discovery and _timestamp(item['retrievedAt'],ceiling)<_timestamp(parent['completedAt'],ceiling):
                raise ValueError('OFFICIAL_SPECIAL_PARENT_TIME_INVALID')
            selected = parent['normalized']['selected']
            if housing_discovery:
                from app.macro.tier1_housing_discovery import select_tier1_report
                selected=select_tier1_report([v['normalized'] for v in seen.values()])
            if selected is None:
                raise ValueError()
            request = {'url':selected['url'],'period':selected['quarter'] if doc['role']=='property-report' else selected['year'] if doc['role'] in ('special-year-directory','special-quota') else selected['period'],
                       'directoryRunId':parent['runId'],'directoryRawSha256':parent['normalized']['rawSha256']}
        if item['request'] != request:
            raise ValueError()
        _normalized(item['normalized'],doc,request,plan)
        seen[item['documentId']] = item
    attempt = group['latestAttempt']
    if (type(attempt) is not dict or set(attempt) != {'plan','status','errorCode','startedAt','finishedAt'}
        or attempt['status'] not in ('completed','partial','pending','failed','source_pending')):
        raise ValueError()
    validate_plan(attempt['plan'])
    if attempt['plan']['groupId'] != group_id or instant(datetime.fromisoformat(attempt['plan']['scheduledAt'])) > ceiling:
        raise ValueError()
    started = _timestamp(attempt['startedAt'],ceiling)
    if attempt['finishedAt'] is not None and _timestamp(attempt['finishedAt'],ceiling) < started:
        raise ValueError()
    if attempt['status'] == 'failed':
        if attempt['errorCode'] not in FAILURE_CODES or attempt['finishedAt'] is None:
            raise ValueError()
    elif attempt['errorCode'] is not None:
        raise ValueError()
    if archive:
        total=len(plan['directoryDocuments'])+len(plan['documents'])+sum('recovery' in d for d in [*plan['directoryDocuments'],*group['documents']])
        if total>plan['groupPolicy']['maxHttpTotal']:raise ValueError('HISTORY_TOTAL_BUDGET_INVALID')
        if attempt['plan']!=plan or attempt['status']!='completed' or len(group['documents'])!=len(plan['documents']):raise ValueError('HISTORY_REPORT_GROUP_INCOMPLETE')
    if housing_discovery and attempt['plan']==plan:
        from app.macro.tier1_housing_discovery import select_tier1_report
        missing=(len(group['documents'])==2 and select_tier1_report([v['normalized'] for v in group['documents']]) is None)
        if (attempt['status']=='source_pending')!=missing or (attempt['status']=='completed' and len(group['documents'])!=3):raise ValueError('OFFICIAL_TIER1_FALSE_COMPLETION')
    if special_discovery and attempt['plan']==plan:
        if attempt['status']=='source_pending' and (not group['documents'] or group['documents'][-1]['normalized'].get('selected','present') is not None):raise ValueError('OFFICIAL_SPECIAL_FALSE_ABSENCE')
        if group['documents'] and group['documents'][-1]['normalized'].get('selected','present') is None:
            if attempt['status']!='source_pending':raise ValueError()
        if attempt['status']=='completed' and len(group['documents'])!=len(plan['documents']):raise ValueError()


def _recovery_document(item,doc,plan,ceiling):
    from app.macro.housing_history_plan import REPORT_VERSION
    if plan['normalizerVersion']==REPORT_VERSION:
        from app.macro.housing_history_recovery import validate_public_recovery
        validate_public_recovery(item['recovery'],run_key(plan,doc),item['runId'],item['retrievedAt'],item['completedAt'],plan,doc)
        _timestamp(item['recovery']['parentFailedAt'],ceiling);_timestamp(item['recovery']['recoveryStartedAt'],ceiling)
        return
    from uuid import uuid5
    from app.macro.official_access_check import NAMESPACE as CHECK_NAMESPACE
    from app.macro.official_access_replay import replay_key
    proof=item['recovery'];parent=run_key(plan,doc)
    if (plan['groupId']!='householdCredit' or doc['role']!='household-ytd' or doc['derivedFrom'] is not None
            or type(proof) is not dict or set(proof)!={'parentRunId','accessCheckRunId','parentEvidenceHash','accessCheckEvidenceHash',
                'parentFailedAt','accessCheckCompletedAt','additionalHttpCount','replayHttpCount'}
            or proof['parentRunId']!=parent or proof['accessCheckRunId']!=str(uuid5(CHECK_NAMESPACE,parent))
            or item['runId']!=replay_key(parent) or type(proof['additionalHttpCount']) is not int or proof['additionalHttpCount']!=1
            or type(proof['replayHttpCount']) is not int or proof['replayHttpCount']!=0
            or any(type(proof[k]) is not str or re.fullmatch('[0-9a-f]{64}',proof[k]) is None for k in ('parentEvidenceHash','accessCheckEvidenceHash'))):
        raise ValueError('OFFICIAL_REPLAY_PUBLIC_INVALID')
    if not (instant(datetime.fromisoformat(plan['scheduledAt']))<=_timestamp(proof['parentFailedAt'],ceiling)
            <=_timestamp(item['retrievedAt'],ceiling)<=_timestamp(proof['accessCheckCompletedAt'],ceiling)<=_timestamp(item['completedAt'],ceiling)):
        raise ValueError('OFFICIAL_REPLAY_PUBLIC_INVALID')


def _grid(plan, shape, values):
    sid,name,unit,frequency = shape
    if plan is None:
        return {'id':sid,'label':name,'unit':unit,'frequency':frequency,'points':[],'missingPeriods':[],'latest':None}
    start = date.fromisoformat(plan['startPeriod'])
    index = start.year*12+start.month-1
    points = []
    width=plan['groupPolicy'].get('lookbackPeriods',(date.fromisoformat(plan['endPeriod']).year*12+date.fromisoformat(plan['endPeriod']).month)-(start.year*12+start.month)+1)
    for i in range(width):
        month = f'{(index+i)//12:04}-{(index+i)%12+1:02}-01'
        points.append({'month':month,'value':values.get(month)})
    present = [p for p in points if p['value'] is not None]
    return {'id':sid,'label':name,'unit':unit,'frequency':frequency,'points':points,
            'missingPeriods':[p['month'] for p in points if p['value'] is None and
                (frequency != 'quarterly' or p['month'][5:7] in ('03','06','09','12'))],
            'latest':present[-1] if present else None}


def _historical_groups(history, group, group_id, day, ceiling):
    """历史组逐组重算旧合同，无嵌套；不凭一个附加摘要信任旧月。"""
    directory_roles={'householdCredit':'household-directory','specialBonds':'special-directory','tier1Housing':'tier1-directory'}
    from app.macro.housing_history_plan import REPORT_VERSION
    archive_only=(group_id=='tier1Housing' and type(history) is list and bool(history)
        and all(type(h) is dict and type(h.get('plan')) is dict and h['plan'].get('normalizerVersion')==REPORT_VERSION for h in history))
    if (group_id not in directory_roles or group['plan'] is None
            or (not archive_only and not any(d['role']==directory_roles[group_id] for d in group['plan']['documents']))
            or type(history) is not list or len(history)>group['plan']['groupPolicy']['lookbackPeriods']+1):
        raise ValueError('OFFICIAL_HOUSEHOLD_HISTORY_INVALID')
    prior=None
    start=date.fromisoformat(group['plan']['startPeriod']);baseline=(start-timedelta(days=1)).replace(day=1).isoformat()
    current=instant(datetime.fromisoformat(group['plan']['scheduledAt']))
    completed=max((_timestamp(d['completedAt'],ceiling) for d in group['documents']),default=current)
    for old in history:
        if type(old) is not dict or set(old)!={'plan','documents','latestAttempt','series','progress'}:
            raise ValueError('OFFICIAL_HOUSEHOLD_HISTORY_INVALID')
        raw={k:v for k,v in old.items() if k not in ('series','progress')}
        empty={key:dict(plan=None,documents=[],latestAttempt=None) for key in OFFICIAL_MACRO_GROUPS}
        empty[group_id]=raw
        rebuilt=build_official_macro_view(dict(analysisDate=day.isoformat(),groups=empty))['groups'][group_id]
        if canonical(rebuilt)!=canonical(old) or old['plan'] is None:
            raise ValueError('OFFICIAL_HOUSEHOLD_HISTORY_INVALID')
        scheduled=instant(datetime.fromisoformat(old['plan']['scheduledAt']))
        from app.macro.housing_history_plan import REPORT_VERSION
        archive=old['plan']['normalizerVersion']==REPORT_VERSION
        if ((not archive and scheduled>=current) or (prior is not None and scheduled<=prior)
                or len(old['documents'])!=len(old['plan']['documents'])
                or old['latestAttempt']['status']!='completed' or old['latestAttempt']['plan']!=old['plan']
                or (not archive and any(_timestamp(d['completedAt'],ceiling)>completed for d in old['documents']))
                or not any(baseline<=p['period']<=group['plan']['endPeriod']
                    for d in old['documents'] for p in d['normalized'].get('points',[]))):
            raise ValueError('OFFICIAL_HOUSEHOLD_HISTORY_INVALID')
        prior=scheduled
    return history


def build_official_macro_view(inputs):
    if type(inputs) is not dict or set(inputs) != {'analysisDate','groups'} or type(inputs['groups']) is not dict or set(inputs['groups']) != OFFICIAL_MACRO_GROUPS:
        raise ValueError('OFFICIAL_MACRO_VIEW_INVALID')
    day = date.fromisoformat(inputs['analysisDate'])
    if day.isoformat() != inputs['analysisDate']:
        raise ValueError('OFFICIAL_MACRO_VIEW_INVALID')
    ceiling = datetime.combine(day+timedelta(days=1),time(),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)-timedelta(microseconds=1)
    groups = {}
    for group_id in sorted(OFFICIAL_MACRO_GROUPS):
        group = deepcopy(inputs['groups'][group_id])
        has_history='history' in group
        history=group.pop('history',[])
        _documents(group,group_id,ceiling)
        if has_history:
            history=_historical_groups(history,group,group_id,day,ceiling)
        values = {}
        versions={}
        for evidence in [*history,group]:
            seen=set()
            for doc in evidence['documents']:
                for point in doc['normalized'].get('points',[]):
                    key=point['period'][:7]+'-01';identity=(point['seriesId'],key)
                    if identity in seen:raise ValueError('OFFICIAL_MACRO_DUPLICATE_PERIOD')
                    seen.add(identity)
                    revision=_timestamp(doc['completedAt'],ceiling)
                    previous=versions.get(identity)
                    if previous and previous[0]==revision and previous[1]!=point['value']:
                        raise ValueError('OFFICIAL_HOUSEHOLD_REVISION_CONFLICT')
                    if previous is None or revision>previous[0]:
                        values.setdefault(point['seriesId'],{})[key]=point['value']
                        versions[identity]=(revision,point['value'])
        if group_id == 'householdCredit':
            ytd = values.get(SERIES[group_id][0][0],{})
            values[SERIES[group_id][1][0]] = {p['month']:p['value'] for p in derive_household_months(ytd)}
        series = [_grid(group['plan'],shape,values.get(shape[0],{})) for shape in SERIES[group_id]]
        progress = None
        if group_id == 'specialBonds' and series[1]['latest']:
            latest = series[1]['latest']
            quota_values = values.get(SHAPES['special-quota'][0][0],{})
            quota = quota_values.get(latest['month'][:4]+'-01-01')
            direct = values.get(SERIES[group_id][0][0],{}).get(latest['month'])
            current = date.fromisoformat(latest['month'])
            previous = (current-timedelta(days=1)).replace(day=1).isoformat()
            prior = values.get(SERIES[group_id][1][0],{}).get(previous) if previous[:4]==latest['month'][:4] else None
            reported = values.get(SERIES[group_id][2][0],{}).get(latest['month'])
            if quota is not None and direct is not None:
                progress = {'period':latest['month'],**derive_special_progress(quota,latest['value'],direct,prior,reported)}
        groups[group_id] = {**group,**({'history':history} if has_history else {}),'series':series,'progress':progress}
    result = {'schemaVersion':'1.0','algorithmVersion':'official-housing-fiscal-v1','basis':BASIS,
              'analysisDate':inputs['analysisDate'],'groups':groups}
    return {**result,'evidenceHash':digest(result)}


def official_macro_view_valid(value):
    try:
        if type(value) is not dict or set(value) != {'schemaVersion','algorithmVersion','basis','analysisDate','groups','evidenceHash'}:
            return False
        inputs = {'analysisDate':value['analysisDate'],'groups':{
            key:{k:v for k,v in group.items() if k not in ('series','progress')} for key,group in value['groups'].items()}}
        return canonical(build_official_macro_view(inputs)) == canonical(value)
    except (ValueError,TypeError,KeyError,StopIteration,AttributeError,ArithmeticError,OverflowError):
        return False


def load_official_macro_view(factory, as_of):
    cutoff = instant(as_of)
    store = OfficialMacroStore(factory)
    groups = {key:{'plan':None,'documents':[],'latestAttempt':None} for key in sorted(OFFICIAL_MACRO_GROUPS)}
    roots = {key:{} for key in OFFICIAL_MACRO_GROUPS}
    with factory() as session:
        rows = session.scalars(select(JobRun).where(JobRun.job_name.in_([JOB+g for g in OFFICIAL_MACRO_GROUPS]),
            JobRun.started_at <= cutoff).order_by(JobRun.started_at,JobRun.id)).all()
        for row in rows:
            row=store.effective_run(session,row,cutoff)
            d = store._run(session,row)
            group_roots = roots[d['plan']['groupId']]
            group = group_roots.setdefault(d['plan']['scheduledAt'],
                {'plan':deepcopy(d['plan']),'documents':[],'latestAttempt':None})
            visible = row.finished_at is not None and utc(row.finished_at) <= cutoff
            phase = d['phase'] if visible else 'pending'
            group['latestAttempt'] = {'plan':deepcopy(d['plan']),'status':'failed' if phase=='failed' else 'partial' if phase=='completed' else 'pending',
                'errorCode':row.error_message if phase=='failed' else None,'startedAt':utc(row.started_at).isoformat(),
                'finishedAt':utc(row.finished_at).isoformat() if visible else None}
            if phase == 'completed' and d['document']['role'] in ('property-directory','household-directory','special-directory','special-budget-directory','special-year-directory') and d['normalized']['selected'] is None:
                group['latestAttempt']['status'] = 'source_pending'
            if phase == 'completed':
                _,retrieved = store._raw(session,row)
                if retrieved <= cutoff:
                    group['documents'].append({'runId':row.id,'documentId':d['document']['id'],'request':deepcopy(d['request']),
                        'retrievedAt':retrieved.isoformat(),'completedAt':utc(row.finished_at).isoformat(),
                        'normalized':deepcopy(d['normalized']),**({'recovery':deepcopy(d['recovery'])} if 'recovery' in d else {})})
    from app.macro.jobs.housing_history_runtime import load_history_group
    archive=load_history_group(factory,cutoff)
    if not rows and archive is None:return None
    for group_id, versions in roots.items():
        if not versions:
            continue
        ordered = [versions[k] for k in sorted(versions)]
        for g in ordered:
            order = {d['id']:i for i,d in enumerate(g['plan']['documents'])}
            g['documents'].sort(key=lambda d:order[d['documentId']])
            if group_id=='tier1Housing' and g['plan']['documents'][0]['role']=='tier1-directory' and len(g['documents'])==2:
                from app.macro.tier1_housing_discovery import select_tier1_report
                if select_tier1_report([v['normalized'] for v in g['documents']]) is None and g['latestAttempt']['status']=='partial':g['latestAttempt']['status']='source_pending'
        complete = [g for g in ordered if len(g['documents']) == len(g['plan']['documents'])]
        for g in complete:
            g['latestAttempt']['status'] = 'completed'
        selected = complete[-1] if complete else ordered[-1]
        # 房价两页已完整检查而本期无候选：公开本期目录/目标月，旧值通过history保留。
        latest=ordered[-1]
        if (group_id=='tier1Housing' and latest['plan']['documents'][0]['role']=='tier1-directory'
            and len(latest['documents'])==2 and latest['latestAttempt']['status']=='source_pending'):
            selected=latest
        groups[group_id] = {**selected,'latestAttempt':ordered[-1]['latestAttempt']}
        directory_roles={'householdCredit':'household-directory','specialBonds':'special-directory','tier1Housing':'tier1-directory'}
        if group_id in directory_roles and any(d['role']==directory_roles[group_id] for d in selected['plan']['documents']):
            first=date.fromisoformat(selected['plan']['startPeriod'])
            baseline=(first-timedelta(days=1)).replace(day=1).isoformat()
            latest={}
            for old in complete:
                for doc in old['documents']:
                    for point in doc['normalized'].get('points',[]):
                        if baseline<=point['period']<=selected['plan']['endPeriod']:
                            rank=instant(datetime.fromisoformat(doc['completedAt']))
                            identity=(point['seriesId'],point['period'])
                            prior=latest.get(identity)
                            if prior is None or rank>prior[0]:latest[identity]=(rank,old)
            chosen={g['plan']['fingerprint']:g for _,g in latest.values() if g is not selected}
            history=[]
            for old in sorted(chosen.values(),key=lambda g:instant(datetime.fromisoformat(g['plan']['scheduledAt']))):
                empty={key:dict(plan=None,documents=[],latestAttempt=None) for key in OFFICIAL_MACRO_GROUPS}
                empty[group_id]=old
                history.append(build_official_macro_view(dict(analysisDate=cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),groups=empty))['groups'][group_id])
            groups[group_id]['history']=history
    if archive is not None:
        group=groups['tier1Housing']
        if group['plan'] is None:
            groups['tier1Housing']={k:v for k,v in archive.items() if k not in ('series','progress')}
        elif any(group['plan']['startPeriod']<=p['period']<=group['plan']['endPeriod'] for d in archive['documents'] for p in d['normalized']['points']):
            old=group.get('history',[])
            group['history']=sorted([*old,archive],key=lambda g:instant(datetime.fromisoformat(g['plan']['scheduledAt'])))
    return build_official_macro_view({'analysisDate':cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),'groups':groups})
