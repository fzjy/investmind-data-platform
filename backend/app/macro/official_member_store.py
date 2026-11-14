"""官方成员独立Ops/Raw账本；预留不退款，已存原件仅零HTTP解析。"""
import base64
from datetime import date,datetime,timezone
from hashlib import sha256
import re
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch,SourcePayload
from app.macro.extensions.plan import digest
from app.macro.extensions.store import FAILURE_CODES,instant
from app.macro.extensions.ingest import utc
from app.macro.official_member_policy import OfficialMemberPolicy
from app.macro.official_members import parse_csi_members,parse_fund_mapping,parse_fund_pcf

JOB='enterprise-official-members-v1'
NAMESPACE=UUID('40f70fc3-3663-5f32-b9dc-b63a1e7c4bb9')


def build_plan(policy,catalog_fingerprint,now):
    day=instant(now).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    value={'schemaVersion':1,'businessDate':day,'catalogFingerprint':catalog_fingerprint,
        'policy':policy.model_dump(),'policyHash':policy.fingerprint}
    result={**value,'fingerprint':digest(value)};validate_plan(result);return result


def validate_plan(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','businessDate','catalogFingerprint','policy','policyHash','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=1
            or date.fromisoformat(plan['businessDate']).isoformat()!=plan['businessDate']
            or re.fullmatch('[0-9a-f]{64}',plan['catalogFingerprint']) is None):raise ValueError()
        p=OfficialMemberPolicy.model_validate(plan['policy'])
        if p.fingerprint!=plan['policyHash'] or digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:
            raise ValueError()
        return p
    except Exception:raise ValueError('OFFICIAL_MEMBER_PLAN_INVALID') from None


def run_key(plan,document):
    if plan.get('schemaVersion')==2:
        from app.macro.catalog_member_store import catalog_run_key
        return catalog_run_key(plan,document)
    return str(uuid5(NAMESPACE,plan['businessDate']+':'+document['id']))


def normalize_source(plan,document,body):
    if document['kind']=='csi-members':
        value=parse_csi_members(body,document['indexCode'],max_members=plan['policy']['maxSnapshotMembers'])
        if value['asOfDate']>plan['businessDate']:raise ValueError('OFFICIAL_MEMBER_FUTURE_DATE')
        return value
    if document['kind']=='fund-mapping':return parse_fund_mapping(body,document['fundCode'])
    if document['kind']=='fund-pcf':
        if document['tradingDate']>plan['businessDate']:raise ValueError('OFFICIAL_MEMBER_FUTURE_DATE')
        return parse_fund_pcf(body,document['fundCode'],document['tradingDate'],max_members=plan['policy']['maxSnapshotMembers'])
    # 先保存官方原件，不猜未经核实的HTML/XML结构，不开放未证明目标。
    raise ValueError('OFFICIAL_MEMBER_NORMALIZER_PENDING')


class OfficialMemberStore:
    def __init__(self,factory,*,daily_budget_enabled=True):
        if type(daily_budget_enabled) is not bool:raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID')
        self.factory=factory
        self.daily_budget_enabled=daily_budget_enabled

    def budget(self,session,now,limit):
        from app.macro.official_member_budget import daily_budget
        return daily_budget(session,now,limit if self.daily_budget_enabled else None)

    def job_name(self,plan):return JOB

    def business_key(self,plan,document):return f"official-members:{plan['businessDate']}:{document['id']}"

    def documents(self,plan):return plan['policy']['documents']

    def run_key(self,plan,document):return run_key(plan,document)

    def max_response_bytes(self,plan):return plan['policy']['maxResponseBytes']

    def dataset(self,plan):return 'enterprise-official-members'

    def api_name(self,plan,document):return 'official-members/'+document['kind']

    def normalize(self,plan,document,body):return normalize_source(plan,document,body)

    def _root(self,session,plan):
        validate_plan(plan)
        root=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key==plan['businessDate']))
        if root is None or root.cursor_value!={'plan':plan}:raise ValueError('OFFICIAL_MEMBER_ROOT_INVALID')
        return root

    def _raw(self,session,row):
        d=row.details;p=d['plan'];doc=d['document']
        if type(d['rawId']) is not int or d['rawId']<=0:raise ValueError('OFFICIAL_MEMBER_RAW_INVALID')
        raw=session.get(SourcePayload,d['rawId'])
        if (raw is None or raw.batch_id!=row.id or raw.api_name!=self.api_name(p,doc)
            or raw.request_hash!=digest({'plan':p['fingerprint'],'document':doc})):
            raise ValueError('OFFICIAL_MEMBER_RAW_INVALID')
        value=raw.payload
        if (type(value) is not dict or set(value)!={'schemaVersion','planFingerprint','document','bodyBase64','rawSha256','retrievedAt'}
            or type(value['schemaVersion']) is not int or value['schemaVersion']!=1
            or value['planFingerprint']!=p['fingerprint'] or value['document']!=doc
            or type(value['bodyBase64']) is not str or len(value['bodyBase64'])>(self.max_response_bytes(p)+2)//3*4
            or digest(value)!=raw.content_hash):raise ValueError('OFFICIAL_MEMBER_RAW_INVALID')
        body=base64.b64decode(value['bodyBase64'],validate=True)
        retrieved=instant(datetime.fromisoformat(value['retrievedAt']))
        if (not body or len(body)>self.max_response_bytes(p) or sha256(body).hexdigest()!=value['rawSha256']
            or retrieved.isoformat()!=value['retrievedAt'] or retrieved!=utc(raw.captured_at)
            or retrieved<utc(row.started_at)):raise ValueError('OFFICIAL_MEMBER_RAW_INVALID')
        return body,retrieved

    def _run(self,session,row):
        if row is None:raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
        d=row.details
        if type(d) is not dict or set(d)!={'schemaVersion','plan','document','phase','rawId','normalized'} or type(d['schemaVersion']) is not int or d['schemaVersion']!=1:
            raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
        p,doc,phase=d['plan'],d['document'],d['phase'];self._root(session,p)
        if (doc not in self.documents(p) or phase not in ('reserved','received','completed','failed')
            or row.id!=self.run_key(p,doc) or row.job_name!=self.job_name(p) or row.business_key!=self.business_key(p,doc)
            or row.request_count!=1 or row.row_count<0):raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
        finished=phase in ('completed','failed')
        if (row.status!=({'reserved':'running','received':'running'}.get(phase,phase))
            or finished!=(row.finished_at is not None)
            or (row.finished_at is not None and utc(row.finished_at)<utc(row.started_at))
            or (row.error_message not in FAILURE_CODES if phase=='failed' else row.error_message is not None)
            or (phase!='completed' and (d['normalized'] is not None or row.row_count))
            or (phase in ('failed','reserved') and d['rawId'] is not None)):
            raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
        batch=session.get(IngestionBatch,row.id)
        if (batch is None or batch.source!='official' or batch.dataset!=self.dataset(p)
            or batch.request_count!=1 or batch.row_count!=row.row_count or utc(batch.started_at)!=utc(row.started_at)
            or batch.status!={'reserved':'running','received':'processing','completed':'completed','failed':'failed'}[phase]
            or (batch.finished_at is None)!=(row.finished_at is None)
            or (batch.finished_at is not None and utc(batch.finished_at)!=utc(row.finished_at))):
            raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
        if phase in ('received','completed'):
            body,retrieved=self._raw(session,row)
            if phase=='completed':
                normalized=self.normalize(p,doc,body)
                if (d['normalized']!=normalized or row.row_count!=normalized.get('memberCount',0)
                    or utc(row.finished_at)<retrieved):raise ValueError('OFFICIAL_MEMBER_PROOF_INVALID')
        return d

    def inspect(self,plan,now):
        now=instant(now);validate_plan(plan)
        today=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if plan['businessDate']>today:raise ValueError('OFFICIAL_MEMBER_CLOCK_REGRESSION')
        with self.factory() as session:
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==JOB).order_by(JobCursor.cursor_key)).all()
            jobs=session.scalars(select(JobRun).where(JobRun.job_name==JOB).order_by(JobRun.started_at,JobRun.id)).all()
            by_date={}
            for root in roots:
                if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('OFFICIAL_MEMBER_ROOT_INVALID')
                old=root.cursor_value['plan'];self._root(session,old)
                if old['businessDate']!=root.cursor_key or old['businessDate']>today:raise ValueError('OFFICIAL_MEMBER_CLOCK_REGRESSION')
                by_date[root.cursor_key]=(old,[])
            for row in jobs:
                d=self._run(session,row)
                if utc(row.started_at)>now or (row.finished_at is not None and utc(row.finished_at)>now):raise ValueError('OFFICIAL_MEMBER_CLOCK_REGRESSION')
                by_date[d['plan']['businessDate']][1].append(row)
            chosen,selected=plan,[]
            for old,rows in by_date.values():
                if not rows or len(rows)>old['policy']['maxHttpPerDay']:raise ValueError('OFFICIAL_MEMBER_ROOT_INVALID')
                if old['businessDate']==plan['businessDate']:
                    if old!=plan:raise ValueError('OFFICIAL_MEMBER_POLICY_CHANGED')
                    chosen,selected=old,rows
            for old,rows in by_date.values():
                if len(rows)<len(old['policy']['documents']) or any(r.details['phase']!='completed' for r in rows):
                    if old['policyHash']!=plan['policyHash'] or old['catalogFingerprint']!=plan['catalogFingerprint']:
                        raise ValueError('OFFICIAL_MEMBER_POLICY_CHANGED')
                    chosen,selected=old,rows;break
            states=[]
            for doc in chosen['policy']['documents']:
                matches=[r for r in selected if r.details['document']==doc]
                if len(matches)>1:raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
                row=matches[0] if matches else None
                states.append({'document':doc,'runId':row.id if row else None,'phase':row.details['phase'] if row else 'new',
                    'errorCode':row.error_message if row else None})
            status='blocked' if any(s['phase'] in ('failed','reserved') for s in states) else 'completed' if all(s['phase']=='completed' for s in states) else 'partial' if selected else 'new'
            return {'status':status,'plan':chosen,'reservedCount':len(selected),'documents':states,
                'dailyBudget':self.budget(session,now,chosen['policy']['maxHttpPerDay'])}

    def reserve(self,plan,document,now):
        now=instant(now);state=self.inspect(plan,now)
        if state['plan']!=plan or state['status']=='blocked' or document not in plan['policy']['documents']:
            raise ValueError('OFFICIAL_MEMBER_RESERVATION_BLOCKED')
        index=plan['policy']['documents'].index(document)
        if (state['documents'][index]['phase']!='new' or state['reservedCount']>=plan['policy']['maxHttpPerDay']
            or any(s['phase']!='completed' for s in state['documents'][:index])):raise ValueError('OFFICIAL_MEMBER_RESERVATION_BLOCKED')
        key=run_key(plan,document)
        with self.factory.begin() as session:
            from app.macro.official_member_budget import family_lock
            family_lock(session)
            if self.budget(session,now,plan['policy']['maxHttpPerDay'])['remaining']==0:
                raise ValueError('OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED')
            root=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key==plan['businessDate']).with_for_update())
            if root is None:
                session.add(JobCursor(job_name=JOB,cursor_key=plan['businessDate'],cursor_value={'plan':plan},updated_at=now));session.flush()
            else:self._root(session,plan)
            rows=session.scalars(select(JobRun).where(JobRun.job_name==JOB)).all()
            same=[r for r in rows if r.details['plan']['businessDate']==plan['businessDate']]
            if (len(same)>=plan['policy']['maxHttpPerDay'] or any(r.details['phase']!='completed' for r in same)):
                raise ValueError('OFFICIAL_MEMBER_RESERVATION_BLOCKED')
            d={'schemaVersion':1,'plan':plan,'document':document,'phase':'reserved','rawId':None,'normalized':None}
            session.add(JobRun(id=key,job_name=JOB,business_key=f"official-members:{plan['businessDate']}:{document['id']}",
                status='running',started_at=now,request_count=1,row_count=0,details=d))
            session.add(IngestionBatch(id=key,source='official',dataset='enterprise-official-members',mode='live',
                status='running',started_at=now,request_count=1,row_count=0,rejected_count=0))
        return key

    def receive(self,key,body,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']!='reserved' or type(body) is not bytes or not body or len(body)>self.max_response_bytes(d['plan']) or now<utc(row.started_at):
                raise ValueError('OFFICIAL_MEMBER_RAW_INVALID')
            payload={'schemaVersion':1,'planFingerprint':d['plan']['fingerprint'],'document':d['document'],
                'bodyBase64':base64.b64encode(body).decode('ascii'),'rawSha256':sha256(body).hexdigest(),'retrievedAt':now.isoformat()}
            raw=SourcePayload(batch_id=key,api_name=self.api_name(d['plan'],d['document']),
                request_hash=digest({'plan':d['plan']['fingerprint'],'document':d['document']}),content_hash=digest(payload),payload=payload,captured_at=now)
            session.add(raw);session.flush()
            row.details={**d,'phase':'received','rawId':raw.id};session.get(IngestionBatch,key).status='processing'

    def fail(self,key,code,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']!='reserved' or code not in FAILURE_CODES or now<utc(row.started_at):raise ValueError('OFFICIAL_MEMBER_RUN_INVALID')
            row.details={**d,'phase':'failed'};row.status='failed';row.finished_at=now;row.error_message=code
            batch=session.get(IngestionBatch,key);batch.status='failed';batch.finished_at=now

    def replay(self,key,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']=='completed':
                if utc(row.finished_at)>now:raise ValueError('OFFICIAL_MEMBER_CLOCK_REGRESSION')
                return {'status':'completed','supplierHttp':0,'readOnly':True,'runId':key}
            if d['phase']!='received':raise ValueError('OFFICIAL_MEMBER_REPLAY_BLOCKED')
            body,retrieved=self._raw(session,row)
            if now<retrieved:raise ValueError('OFFICIAL_MEMBER_CLOCK_REGRESSION')
            normalized=self.normalize(d['plan'],d['document'],body)
            row.details={**d,'phase':'completed','normalized':normalized};row.status='completed';row.finished_at=now
            row.row_count=normalized.get('memberCount',0)
            batch=session.get(IngestionBatch,key);batch.status='completed';batch.finished_at=now;batch.row_count=row.row_count
            return {'status':'completed','supplierHttp':0,'readOnly':False,'runId':key}
