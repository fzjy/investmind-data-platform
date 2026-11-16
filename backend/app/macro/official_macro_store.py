"""官方宏观独立预留/原件/修订账本；失败不退款，received 只重读原件。"""
import base64
from datetime import date, datetime
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import digest, validate_extension_plan
from app.macro.extensions.store import FAILURE_CODES, instant
from app.macro.fiscal_store import core_value
from app.macro.official_macro_normalize import (
    parse_household_ytd, parse_tier1_housing, parse_special_quota,
    parse_special_issuance, parse_property_directory, parse_property_loan_report,
)

JOB = 'macro-official-housing-fiscal-v1:'
DATASET = 'macro-official-housing-fiscal:'
NAMESPACE = UUID('25d93df5-81fc-48ce-b71b-06f431fc1239')


def validate_plan(plan: dict) -> None:
    from app.macro.housing_history_plan import VERSIONS,validate_history_plan
    if type(plan) is dict and plan.get('normalizerVersion') in VERSIONS:
        validate_history_plan(plan)
        return
    validate_extension_plan(plan)
    if plan['groupId'] not in OFFICIAL_MACRO_GROUPS:
        raise ValueError('OFFICIAL_MACRO_PLAN_INVALID')


def run_key(plan: dict, doc: dict) -> str:
    from app.macro.housing_history_plan import VERSIONS
    namespace=uuid5(NAMESPACE,plan['normalizerVersion']) if plan.get('normalizerVersion') in VERSIONS else NAMESPACE
    return str(uuid5(namespace, plan['groupId']+':'+plan['scheduledAt']+':'+doc['id']))


def job_name(plan):
    from app.macro.housing_history_plan import VERSIONS
    prefix=plan['normalizerVersion']+':' if plan.get('normalizerVersion') in VERSIONS else JOB
    return prefix+plan['groupId']


def dataset_name(plan):
    return job_name(plan) if job_name(plan)!=JOB+plan['groupId'] else DATASET+plan['groupId']


def checked_without_candidate(plan, attempts):
    """目录成功但无候选是已完成本周检查，未发送报告不算未知预留。"""
    if plan['groupId']=='tier1Housing' and plan['documents'][0]['role']=='tier1-directory':
        from app.macro.tier1_housing_discovery import select_tier1_report
        ordered=sorted(attempts,key=lambda r:plan['documents'].index(r.details['document']))
        return (len(ordered)==2 and [r.details['document'] for r in ordered]==plan['documents'][:2]
            and all(r.details['phase']=='completed' for r in ordered)
            and select_tier1_report([r.details['normalized'] for r in ordered]) is None)
    if plan['groupId']=='specialBonds' and plan['documents'][0]['role']=='special-budget-directory' and attempts:
        ordered=sorted(attempts,key=lambda r:plan['documents'].index(r.details['document']))
        return ([r.details['document'] for r in ordered]==plan['documents'][:len(ordered)]
            and all(r.details['phase']=='completed' for r in ordered)
            and ordered[-1].details['document']['role'] in ('special-budget-directory','special-year-directory','special-directory')
            and ordered[-1].details['normalized']['selected'] is None)
    return (plan['groupId'] in ('propertyLoans','householdCredit','specialBonds') and len(attempts) == 1
        and attempts[0].details['document']['role'] in ('property-directory','household-directory','special-directory')
        and attempts[0].details['phase'] == 'completed'
        and attempts[0].details['normalized']['selected'] is None)


def normalize_document(plan: dict, doc: dict, request: dict, body: bytes) -> dict:
    role = doc['role']
    if role == 'household-ytd':
        result = parse_household_ytd(body, request['period'])
    elif role == 'household-directory':
        from app.macro.household_discovery import parse_household_directory
        result = parse_household_directory(body, plan['endPeriod'][:7],
            max_candidates=plan['limits']['maxDirectoryCandidates'])
    elif role=='tier1-history-directory':
        from app.macro.housing_history_plan import parse_history_directory
        result=parse_history_directory(body,plan,int(doc['period'][5:]))
    elif role == 'tier1-prices':
        result = parse_tier1_housing(body, request['period'])
    elif role in ('tier1-directory','tier1-directory-page'):
        from app.macro.tier1_housing_discovery import parse_tier1_directory
        result=parse_tier1_directory(body,plan['endPeriod'][:7],page=0 if role=='tier1-directory' else 1,max_candidates=plan['limits']['maxDirectoryCandidates'])
    elif role == 'special-quota':
        result = parse_special_quota(body, int(request['period']))
    elif role in ('special-budget-directory','special-year-directory'):
        from app.macro.special_quota_discovery import parse_quota_directory
        result=parse_quota_directory(body,role,int(plan['endPeriod'][:4]),max_candidates=plan['limits']['maxDirectoryCandidates'])
    elif role in ('special-issuance', 'special-briefing'):
        result = parse_special_issuance(body, request['period'], role)
    elif role=='special-directory':
        from app.macro.special_discovery import parse_special_directory
        result=parse_special_directory(body,plan['endPeriod'][:7],max_candidates=plan['limits']['maxDirectoryCandidates'])
    elif role == 'property-directory':
        result = parse_property_directory(body, plan['latestQuarter'],
            max_candidates=plan['limits']['maxDirectoryCandidates'],
            quarter_fallbacks=plan['groupPolicy']['quarterFallbacks'])
    elif role == 'property-report':
        result = parse_property_loan_report(body, request['period'])
    else:
        raise ValueError('OFFICIAL_MACRO_NORMALIZER_PENDING')
    if len(result.get('points', [])) > plan['limits']['maxResponseRows']:
        raise ValueError('OFFICIAL_MACRO_ROW_LIMIT')
    return result


class OfficialMacroStore:
    def __init__(self, factory):
        self.factory = factory

    def _root(self, session, plan):
        validate_plan(plan)
        root = session.scalar(select(JobCursor).where(
            JobCursor.job_name == job_name(plan), JobCursor.cursor_key == plan['scheduledAt']))
        if root is None or root.cursor_value != {'plan': plan}:
            raise ValueError('OFFICIAL_MACRO_ROOT_INVALID')
        from app.macro.housing_history_plan import REPORT_VERSION
        if plan['normalizerVersion']==REPORT_VERSION:
            verified=session.info.setdefault('housing-history-parent-proofs',set())
            if plan['fingerprint'] not in verified:
                directory=plan['directoryPlan'];self._root(session,directory)
                for proof in plan['directoryDocuments']:
                    parent=session.get(JobRun,proof['runId']);details=self._run(session,parent)
                    if details['plan']!=directory or details['phase']!='completed':raise ValueError('HISTORY_PARENT_NOT_COMPLETED')
                    _,retrieved=self._raw(session,parent)
                    actual=dict(runId=parent.id,documentId=details['document']['id'],request=details['request'],
                        retrievedAt=retrieved.isoformat(),completedAt=utc(parent.finished_at).isoformat(),normalized=details['normalized'])
                    if 'recovery' in details:actual['recovery']=details['recovery']
                    if actual!=proof:raise ValueError('HISTORY_PARENT_PROOF_INVALID')
                verified.add(plan['fingerprint'])
        return root

    def _request(self, session, plan, doc):
        if doc not in plan['documents']:
            raise ValueError('OFFICIAL_MACRO_DOCUMENT_INVALID')
        if doc['derivedFrom'] is None:
            return {'url': doc['url'], 'period': doc['period'], 'directoryRunId': None, 'directoryRawSha256': None}
        parent = next(d for d in plan['documents'] if d['id'] == doc['derivedFrom'])
        row = session.get(JobRun, run_key(plan, parent))
        if row is None:
            return None
        d = self._run(session, row)
        if d['phase'] != 'completed' or (d['normalized']['selected'] is None and doc['role']!='tier1-prices'):
            return None
        selected = d['normalized']['selected']
        if doc['role']=='tier1-prices':
            from app.macro.tier1_housing_discovery import select_tier1_report
            first=session.get(JobRun,run_key(plan,plan['documents'][0]))
            if first is None:return None
            first_details=self._run(session,first)
            if first_details['phase']!='completed':return None
            selected=select_tier1_report([first_details['normalized'],d['normalized']])
            if selected is None:return None
        return {'url': selected['url'], 'period': selected['quarter'] if doc['role']=='property-report' else selected['year'] if doc['role'] in ('special-year-directory','special-quota') else selected['period'],
                'directoryRunId': row.id, 'directoryRawSha256': d['normalized']['rawSha256']}

    def request_for(self, plan, doc, now):
        state = self.inspect(plan, now)
        if state['plan'] != plan or state['status'] == 'blocked':
            raise ValueError('OFFICIAL_MACRO_REQUEST_BLOCKED')
        with self.factory() as session:
            return self._request(session, plan, doc)

    def _raw(self, session, row):
        if row.job_name.startswith('macro-official-access-replay-v1:'):
            from app.macro.official_access_replay import read_replay_raw
            return read_replay_raw(self,session,row)
        d = row.details
        p, doc = d['plan'], d['document']
        if type(d['rawId']) is not int or d['rawId'] <= 0:
            raise ValueError('OFFICIAL_MACRO_RAW_INVALID')
        raw = session.get(SourcePayload, d['rawId'])
        request_hash = digest({'plan': p['fingerprint'], 'document': doc, 'request': d['request']})
        if raw is None or raw.batch_id != row.id or raw.api_name != 'official-macro/'+doc['role'] or raw.request_hash != request_hash:
            raise ValueError('OFFICIAL_MACRO_RAW_INVALID')
        v = raw.payload
        if (type(v) is not dict or set(v) != {'schemaVersion','planFingerprint','document','request','bodyBase64','rawSha256','retrievedAt','releaseTimeBasis'}
            or type(v['schemaVersion']) is not int or v['schemaVersion'] != 1 or v['planFingerprint'] != p['fingerprint']
            or v['document'] != doc or v['request'] != d['request'] or v['releaseTimeBasis'] != 'normalization-completion-not-source-release'
            or type(v['bodyBase64']) is not str or len(v['bodyBase64']) > (p['limits']['maxResponseBytes']+2)//3*4
            or digest(v) != raw.content_hash):
            raise ValueError('OFFICIAL_MACRO_RAW_INVALID')
        body = base64.b64decode(v['bodyBase64'], validate=True)
        retrieved = instant(datetime.fromisoformat(v['retrievedAt']))
        if (not body or len(body) > p['limits']['maxResponseBytes'] or sha256(body).hexdigest() != v['rawSha256']
            or retrieved.isoformat() != v['retrievedAt'] or retrieved != utc(raw.captured_at) or retrieved < utc(row.started_at)):
            raise ValueError('OFFICIAL_MACRO_RAW_INVALID')
        return body, retrieved

    def _check_core(self, session, row):
        d = row.details
        points = d['normalized'].get('points', [])
        if len(points) != len(d['coreBindings']) or row.row_count != len(points):
            raise ValueError('OFFICIAL_MACRO_CORE_INVALID')
        for point, binding in zip(points, d['coreBindings'], strict=True):
            if type(binding) is not dict or set(binding) != {'coreId','seriesId','period','value','revision'} or type(binding['coreId']) is not int:
                raise ValueError('OFFICIAL_MACRO_CORE_INVALID')
            core = session.get(MacroObservation, binding['coreId'])
            revision = digest({'runId': row.id, 'rawSha256': d['normalized']['rawSha256'], 'point': point})
            if (core is None or core.source != d['normalized']['source'] or core.series_id != point['seriesId']
                or core.region != 'CN' or core.seasonal_adjustment != 'NSA' or core.frequency != point['frequency']
                or core.name != point['name'] or core.unit != point['unit'] or core.source_url != d['request']['url']
                or core.observed_at.isoformat() != point['period'] or core.value != core_value(point['value'])
                or core.revision != revision or utc(core.released_at) != utc(row.finished_at)
                or (core.valid_until is not None and utc(core.valid_until) <= utc(row.finished_at))
                or binding != {'coreId':core.id,'seriesId':core.series_id,'period':point['period'],'value':str(core.value),'revision':revision}):
                raise ValueError('OFFICIAL_MACRO_CORE_INVALID')

    def _run(self, session, row):
        if row is None:
            raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
        from app.macro.housing_history_recovery import JOB as RECOVERY_JOB,validate_recovery_run
        if row.job_name==RECOVERY_JOB:return validate_recovery_run(self,session,row)
        if row.job_name.startswith('macro-official-access-replay-v1:'):
            from app.macro.official_access_replay import validate_replay_run
            return validate_replay_run(self,session,row)
        d = row.details
        if (type(d) is not dict or set(d) != {'schemaVersion','plan','document','request','phase','rawId','normalized','coreBindings'}
            or type(d['schemaVersion']) is not int or d['schemaVersion'] != 1):
            raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
        p, doc, phase = d['plan'], d['document'], d['phase']
        self._root(session, p)
        if (doc not in p['documents'] or row.id != run_key(p, doc) or row.job_name != job_name(p)
            or row.business_key != p['groupId']+':'+p['scheduledAt']+':'+doc['id'] or row.request_count != 1
            or phase not in ('reserved','received','completed','failed') or d['request'] != self._request(session,p,doc)
            or d['request'] is None or type(d['coreBindings']) is not list):
            raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
        finished = phase in ('completed','failed')
        if (row.status != ('running' if not finished else phase) or finished != (row.finished_at is not None)
            or (finished and utc(row.finished_at) < utc(row.started_at))
            or (row.error_message not in FAILURE_CODES if phase == 'failed' else row.error_message is not None)
            or (phase != 'completed' and (d['normalized'] is not None or d['coreBindings'] or row.row_count))
            or (phase in ('reserved','failed') and d['rawId'] is not None)):
            raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
        batch = session.get(IngestionBatch,row.id)
        if (batch is None or batch.source != 'official' or batch.dataset != dataset_name(p) or batch.mode != 'live'
            or batch.request_count != 1 or batch.row_count != row.row_count or batch.rejected_count != 0
            or utc(batch.started_at) != utc(row.started_at)
            or batch.status != {'reserved':'running','received':'processing','completed':'completed','failed':'failed'}[phase]
            or (batch.finished_at is None) != (row.finished_at is None)
            or (batch.finished_at is not None and utc(batch.finished_at) != utc(row.finished_at))):
            raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
        if phase in ('received','completed'):
            body, retrieved = self._raw(session,row)
            if phase == 'completed':
                if utc(row.finished_at) < retrieved or d['normalized'] != normalize_document(p,doc,d['request'],body):
                    raise ValueError('OFFICIAL_MACRO_PROOF_INVALID')
                self._check_core(session,row)
        return d

    def effective_run(self,session,row,as_of):
        from app.macro.housing_history_plan import VERSIONS
        if (row.details.get('plan',{}).get('normalizerVersion') in VERSIONS and row.details.get('phase')=='failed'
            and row.error_message in ('EXTENSION_TIMEOUT','EXTENSION_NETWORK_ERROR')):
            from app.macro.housing_history_recovery import effective_recovery
            return effective_recovery(self,session,row,as_of)
        d=self._run(session,row)
        if row.job_name==JOB+'householdCredit' and d['phase']=='failed' and row.error_message=='EXTENSION_AUTH_ERROR':
            from app.macro.official_access_replay import effective_replay
            return effective_replay(self,session,row,as_of)
        return row

    def inspect(self, plan, now):
        now = instant(now)
        validate_plan(plan)
        if instant(datetime.fromisoformat(plan['scheduledAt'])) > now:
            raise ValueError('OFFICIAL_MACRO_CLOCK_REGRESSION')
        job = job_name(plan)
        with self.factory() as session:
            roots = session.scalars(select(JobCursor).where(JobCursor.job_name==job).order_by(JobCursor.cursor_key)).all()
            rows = session.scalars(select(JobRun).where(JobRun.job_name==job).order_by(JobRun.started_at,JobRun.id)).all()
            by_root = {}
            for root in roots:
                if type(root.cursor_value) is not dict or set(root.cursor_value) != {'plan'}:
                    raise ValueError('OFFICIAL_MACRO_ROOT_INVALID')
                old = root.cursor_value['plan']
                self._root(session,old)
                if old['groupId'] != plan['groupId'] or root.cursor_key != old['scheduledAt'] or instant(datetime.fromisoformat(old['scheduledAt'])) > now:
                    raise ValueError('OFFICIAL_MACRO_ROOT_INVALID')
                by_root[root.cursor_key] = (old,[])
            for row in rows:
                row=self.effective_run(session,row,now)
                d = self._run(session,row)
                if utc(row.started_at) > now or (row.finished_at is not None and utc(row.finished_at) > now):
                    raise ValueError('OFFICIAL_MACRO_CLOCK_REGRESSION')
                if d['rawId'] is not None and self._raw(session,row)[1] > now:
                    raise ValueError('OFFICIAL_MACRO_CLOCK_REGRESSION')
                by_root[d['plan']['scheduledAt']][1].append(row)
            chosen, selected = plan, []
            for old, attempts in by_root.values():
                if not attempts or len(attempts) > old['limits']['maxHttpPerPeriod']:
                    raise ValueError('OFFICIAL_MACRO_ROOT_INVALID')
                if old['scheduledAt'] == plan['scheduledAt']:
                    if old != plan:
                        terminal=checked_without_candidate(old,attempts) or (len(attempts)==len(old['documents']) and all(r.details['phase']=='completed' for r in attempts))
                        upgrade=(plan['groupId']=='specialBonds' and plan['documents'][0]['role']=='special-budget-directory') or (plan['groupId']=='tier1Housing' and plan['documents'][0]['role']=='tier1-directory')
                        if not (upgrade and terminal):raise ValueError('OFFICIAL_MACRO_POLICY_CHANGED')
                    chosen,selected = old,attempts
            for old,attempts in by_root.values():
                if not checked_without_candidate(old, attempts) and (len(attempts) < len(old['documents']) or any(r.details['phase'] != 'completed' for r in attempts)):
                    if old['groupPolicyHash'] != plan['groupPolicyHash']:
                        raise ValueError('OFFICIAL_MACRO_POLICY_CHANGED')
                    chosen,selected = old,attempts
                    break
            states = []
            for doc in chosen['documents']:
                matches = [r for r in selected if r.details['document']==doc]
                if len(matches) > 1:
                    raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
                row = matches[0] if matches else None
                states.append({'document':doc,'runId':row.id if row else None,
                               'phase':row.details['phase'] if row else 'new','errorCode':row.error_message if row else None})
                if row is not None and 'recovery' in row.details:
                    states[-1].update(originalRunId=row.details['recovery']['parentRunId'],recovery=row.details['recovery'])
            status = ('blocked' if any(s['phase'] in ('reserved','failed') for s in states) else
                      'source_pending' if checked_without_candidate(chosen, selected) else
                      'completed' if all(s['phase']=='completed' for s in states) else 'partial' if selected else 'new')
            return {'status':status,'plan':chosen,'runId':selected[0].id if selected else None,
                    'reservedCount':len(selected),'documents':states}

    def reserve(self, plan, doc, now):
        now = instant(now)
        state = self.inspect(plan,now)
        if state['plan'] != plan or state['status']=='blocked' or doc not in plan['documents']:
            raise ValueError('OFFICIAL_MACRO_RESERVATION_BLOCKED')
        index = plan['documents'].index(doc)
        if state['documents'][index]['phase'] != 'new' or any(s['phase'] != 'completed' for s in state['documents'][:index]):
            raise ValueError('OFFICIAL_MACRO_RESERVATION_BLOCKED')
        with self.factory.begin() as session:
            root = session.scalar(select(JobCursor).where(JobCursor.job_name==job_name(plan),
                JobCursor.cursor_key==plan['scheduledAt']).with_for_update())
            if root is None:
                session.add(JobCursor(job_name=job_name(plan),cursor_key=plan['scheduledAt'],cursor_value={'plan':plan},updated_at=now))
                session.flush()
            self._root(session,plan)
            from app.macro.housing_history_plan import VERSIONS
            if plan['normalizerVersion'] in VERSIONS:
                base=plan.get('directoryPlan',plan)
                session.scalar(select(JobCursor).where(JobCursor.job_name==job_name(base),JobCursor.cursor_key==base['scheduledAt']).with_for_update())
                from app.macro.housing_history_recovery import reserved_in_session
                if reserved_in_session(session)>=plan['groupPolicy']['maxHttpTotal']:raise ValueError('HISTORY_TOTAL_BUDGET_EXHAUSTED')
            from app.macro.housing_history_plan import REPORT_VERSION
            if plan['normalizerVersion']==REPORT_VERSION and now<instant(datetime.fromisoformat(plan['directoryDocuments'][-1]['completedAt'])):
                raise ValueError('HISTORY_REPORT_BEFORE_DIRECTORY')
            existing = session.scalars(select(JobRun).where(JobRun.job_name==job_name(plan))).all()
            same = [self.effective_run(session,r,now) for r in existing if r.details['plan']['scheduledAt']==plan['scheduledAt']]
            if len(same) != index or len(same) >= plan['limits']['maxHttpPerPeriod'] or any(r.details['phase'] != 'completed' for r in same):
                raise ValueError('OFFICIAL_MACRO_RESERVATION_BLOCKED')
            request = self._request(session,plan,doc)
            if request is None:
                raise ValueError('OFFICIAL_MACRO_NO_OBSERVED_REPORT')
            key = run_key(plan,doc)
            details = {'schemaVersion':1,'plan':plan,'document':doc,'request':request,'phase':'reserved',
                       'rawId':None,'normalized':None,'coreBindings':[]}
            session.add(JobRun(id=key,job_name=job_name(plan),business_key=plan['groupId']+':'+plan['scheduledAt']+':'+doc['id'],
                status='running',started_at=now,request_count=1,row_count=0,details=details))
            session.add(IngestionBatch(id=key,source='official',dataset=dataset_name(plan),mode='live',
                status='running',started_at=now,request_count=1,row_count=0,rejected_count=0))
        return key

    def inventory(self, plan, now):
        """仅已验证冻结根的 Core 绑定；不把同名但无原件的旧行算入。"""
        state = self.inspect(plan, now)
        points = []
        with self.factory() as session:
            for entry in state['documents']:
                if entry['phase'] == 'completed':
                    points.extend(session.get(JobRun, entry['runId']).details['coreBindings'])
        return {'rawRows':len(points),'validVisibleRows':len(points),'invalidRows':0,
                'notVisibleRows':0,'distinctPeriods':len({p['period'] for p in points}),
                'scope':'frozen-plan-core-bindings'}

    def receive(self,key,body,now):
        now = instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True)
            d=self._run(session,row)
            if d['phase']!='reserved' or type(body) is not bytes or not body or len(body)>d['plan']['limits']['maxResponseBytes'] or now<utc(row.started_at):
                raise ValueError('OFFICIAL_MACRO_RAW_INVALID')
            value={'schemaVersion':1,'planFingerprint':d['plan']['fingerprint'],'document':d['document'],'request':d['request'],
                   'bodyBase64':base64.b64encode(body).decode('ascii'),'rawSha256':sha256(body).hexdigest(),
                   'retrievedAt':now.isoformat(),'releaseTimeBasis':'normalization-completion-not-source-release'}
            raw=SourcePayload(batch_id=key,api_name='official-macro/'+d['document']['role'],
                request_hash=digest({'plan':d['plan']['fingerprint'],'document':d['document'],'request':d['request']}),
                content_hash=digest(value),payload=value,captured_at=now)
            session.add(raw)
            session.flush()
            row.details={**d,'phase':'received','rawId':raw.id}
            session.get(IngestionBatch,key).status='processing'

    def fail(self,key,code,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True)
            d=self._run(session,row)
            if d['phase']!='reserved' or code not in FAILURE_CODES or now<utc(row.started_at):
                raise ValueError('OFFICIAL_MACRO_RUN_INVALID')
            row.details={**d,'phase':'failed'}
            row.status='failed';row.finished_at=now;row.error_message=code
            batch=session.get(IngestionBatch,key);batch.status='failed';batch.finished_at=now
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,now)

    def replay(self,key,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True)
            d=self._run(session,row)
            if d['phase']=='completed':
                if utc(row.finished_at)>now:
                    raise ValueError('OFFICIAL_MACRO_CLOCK_REGRESSION')
                return {'status':'completed','supplierHttp':0,'readOnly':True,'runId':key}
            if d['phase']!='received':
                raise ValueError('OFFICIAL_MACRO_REPLAY_BLOCKED')
            body,retrieved=self._raw(session,row)
            if now<retrieved or now<utc(row.started_at):
                raise ValueError('OFFICIAL_MACRO_CLOCK_REGRESSION')
            normalized=normalize_document(d['plan'],d['document'],d['request'],body)
            if d['document']['role']=='tier1-directory-page':
                from app.macro.tier1_housing_discovery import select_tier1_report
                first=session.get(JobRun,run_key(d['plan'],d['plan']['documents'][0]))
                if first is None or first.details['phase']!='completed':raise ValueError('OFFICIAL_TIER1_DIRECTORY_PREFIX_REQUIRED')
                select_tier1_report([self._run(session,first)['normalized'],normalized])
            bindings=[]
            for point in normalized.get('points',[]):
                previous=session.scalars(select(MacroObservation).where(MacroObservation.series_id==point['seriesId'],
                    MacroObservation.observed_at==date.fromisoformat(point['period'])).with_for_update()).all()
                if any(r.source!=normalized['source'] or utc(r.released_at)>=now for r in previous):
                    raise ValueError('OFFICIAL_MACRO_REVISION_INVALID')
                active=[r for r in previous if r.valid_until is None]
                if len(active)>1 or (previous and not active):
                    raise ValueError('OFFICIAL_MACRO_REVISION_INVALID')
                for prior in active:
                    if prior.frequency!=point['frequency'] or prior.unit!=point['unit'] or prior.region!='CN' or prior.seasonal_adjustment!='NSA':
                        raise ValueError('OFFICIAL_MACRO_REVISION_INVALID')
                    prior.valid_until=now
                revision=digest({'runId':key,'rawSha256':normalized['rawSha256'],'point':point})
                core=MacroObservation(source=normalized['source'],series_id=point['seriesId'],name=point['name'],region='CN',
                    frequency=point['frequency'],unit=point['unit'],seasonal_adjustment='NSA',
                    observed_at=date.fromisoformat(point['period']),released_at=now,revision=revision,
                    value=core_value(point['value']),source_url=d['request']['url'])
                session.add(core);session.flush()
                bindings.append({'coreId':core.id,'seriesId':core.series_id,'period':point['period'],'value':str(core.value),'revision':revision})
            row.details={**d,'phase':'completed','normalized':normalized,'coreBindings':bindings}
            row.status='completed';row.finished_at=now;row.row_count=len(bindings)
            batch=session.get(IngestionBatch,key);batch.status='completed';batch.finished_at=now;batch.row_count=len(bindings)
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,now)
            return {'status':'completed','supplierHttp':0,'readOnly':False,'runId':key,'insertedRows':len(bindings)}
