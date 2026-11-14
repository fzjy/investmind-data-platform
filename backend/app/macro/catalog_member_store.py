"""单目标静态官方成员计划；执行账本在同一模块按目标恢复。"""
from datetime import date
from datetime import datetime
import re
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch

from app.macro.catalog_member_policy import derived_policy
from app.macro.enterprise_target_catalog import EnterpriseTarget
from app.macro.extensions.plan import digest
from app.macro.extensions.store import instant
from app.macro.extensions.ingest import utc
from app.macro.official_member_policy import load_official_policy
from app.macro.official_member_store import OfficialMemberStore
from app.macro.official_member_budget import CATALOG_JOB_PREFIX,family_lock

CATALOG_NAMESPACE=UUID('6ed8d693-56de-4af3-8ba0-a529c3a9d07b')


def build_catalog_member_plan(catalog,target_code,now):
    try:
        now=instant(now)
        config=catalog.catalog_member_policy
        scope=next(t for t in config.targets if t.code==target_code)
        target=next(t for t in catalog.targets if t.code==target_code)
        if not scope.documents:raise ValueError('CATALOG_MEMBER_SOURCE_PENDING')
        policy=derived_policy(catalog.official_policy,target,scope)
        value={'schemaVersion':2,'businessDate':now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'catalogFingerprint':catalog.fingerprint,'target':{'code':target.code,'kind':target.kind,'label':target.label,
                'sourceIndexNameHint':target.source_index_name_hint},'policy':policy.model_dump(),'policyHash':policy.fingerprint}
        result={**value,'fingerprint':digest(value)};validate_catalog_member_plan(result);return result
    except (ValueError,TypeError,KeyError,AttributeError,StopIteration):
        raise ValueError('CATALOG_MEMBER_PLAN_INVALID') from None


def validate_catalog_member_plan(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','businessDate','catalogFingerprint','target','policy','policyHash','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=2
            or type(plan['businessDate']) is not str or date.fromisoformat(plan['businessDate']).isoformat()!=plan['businessDate']
            or type(plan['catalogFingerprint']) is not str or re.fullmatch('[0-9a-f]{64}',plan['catalogFingerprint']) is None):raise ValueError()
        target=plan['target']
        if (type(target) is not dict or set(target)!={'kind','code','label','sourceIndexNameHint'}
            or target['kind'] not in ('index','etf') or type(target['code']) is not str
            or re.fullmatch(r'[0-9]{6}\.(SH|SZ)',target['code']) is None
            or type(target['label']) is not str or not target['label'].strip()
            or (target['sourceIndexNameHint'] is not None if target['kind']=='index' else
                type(target['sourceIndexNameHint']) is not str or not target['sourceIndexNameHint'].strip())):raise ValueError()
        item=EnterpriseTarget(target['kind'],target['code'],target['label'],target['sourceIndexNameHint'])
        policy=load_official_policy(plan['policy'],(item,))
        if (not policy.collectionEnabled or policy.bootstrapTargetCodes!=[item.code]
            or policy.fingerprint!=plan['policyHash']
            or any(d.tradingDate is not None and d.tradingDate>plan['businessDate'] for d in policy.documents)
            or digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']):raise ValueError()
        return policy
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise ValueError('CATALOG_MEMBER_PLAN_INVALID') from None


def catalog_run_key(plan,document):
    return str(uuid5(CATALOG_NAMESPACE,plan['target']['code']+':'+document['id']))


class CatalogMemberStore(OfficialMemberStore):
    def job_name(self,plan):return CATALOG_JOB_PREFIX+plan['target']['code']

    def business_key(self,plan,document):return 'official-catalog:'+plan['target']['code']+':'+document['id']

    def _root(self,session,plan):
        validate_catalog_member_plan(plan)
        root=session.scalar(select(JobCursor).where(JobCursor.job_name==self.job_name(plan),JobCursor.cursor_key==plan['target']['code']))
        if root is None or root.cursor_value!={'plan':plan}:raise ValueError('CATALOG_MEMBER_ROOT_INVALID')
        return root

    def inspect(self,plan,now):
        now=instant(now);validate_catalog_member_plan(plan)
        today=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if plan['businessDate']>today:raise ValueError('CATALOG_MEMBER_CLOCK_REGRESSION')
        with self.factory() as session:
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==self.job_name(plan))).all()
            rows=session.scalars(select(JobRun).where(JobRun.job_name==self.job_name(plan))).all()
            if len(roots)>1 or not roots and rows:raise ValueError('CATALOG_MEMBER_ROOT_INVALID')
            chosen=plan
            if roots:
                root=roots[0]
                if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('CATALOG_MEMBER_ROOT_INVALID')
                chosen=root.cursor_value['plan'];self._root(session,chosen)
                if (root.cursor_key!=plan['target']['code'] or chosen['target']!=plan['target']
                    or chosen['catalogFingerprint']!=plan['catalogFingerprint'] or chosen['policyHash']!=plan['policyHash']
                    or chosen['businessDate']>today or utc(root.updated_at)>now):raise ValueError('CATALOG_MEMBER_POLICY_CHANGED')
                if not rows:raise ValueError('CATALOG_MEMBER_ROOT_INVALID')
            indices=[]
            for row in rows:
                d=self._run(session,row)
                if d['plan']!=chosen or utc(row.started_at)>now or row.finished_at is not None and utc(row.finished_at)>now:
                    raise ValueError('CATALOG_MEMBER_CLOCK_REGRESSION')
                if d['rawId'] is not None and self._raw(session,row)[1]>now:raise ValueError('CATALOG_MEMBER_CLOCK_REGRESSION')
                indices.append(chosen['policy']['documents'].index(d['document']))
            if sorted(indices)!=list(range(len(rows))):raise ValueError('CATALOG_MEMBER_ATTEMPTS_INVALID')
            entries=[]
            for doc in chosen['policy']['documents']:
                row=next((r for r in rows if r.details['document']==doc),None)
                entries.append({'document':doc,'runId':row.id if row else None,'phase':row.details['phase'] if row else 'new',
                                'errorCode':row.error_message if row else None})
            state=('blocked' if any(e['phase'] in ('reserved','failed') for e in entries) else
                'completed' if all(e['phase']=='completed' for e in entries) else 'partial' if rows else 'new')
            return {'status':state,'plan':chosen,'reservedCount':len(rows),'documents':entries,
                    'dailyBudget':self.budget(session,now,chosen['policy']['maxHttpPerDay'])}

    def reserve(self,plan,document,now):
        now=instant(now);validate_catalog_member_plan(plan)
        with self.factory.begin() as session:
            family_lock(session);state=self.inspect(plan,now)
            if state['plan']!=plan or state['status']=='blocked' or document not in plan['policy']['documents']:
                raise ValueError('CATALOG_MEMBER_RESERVATION_BLOCKED')
            index=plan['policy']['documents'].index(document)
            if (state['documents'][index]['phase']!='new'
                or any(e['phase']!='completed' for e in state['documents'][:index])):raise ValueError('CATALOG_MEMBER_RESERVATION_BLOCKED')
            if self.budget(session,now,plan['policy']['maxHttpPerDay'])['remaining']==0:
                raise ValueError('OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED')
            root=session.scalar(select(JobCursor).where(JobCursor.job_name==self.job_name(plan),JobCursor.cursor_key==plan['target']['code']).with_for_update())
            if root is None:
                if plan['businessDate']!=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
                    raise ValueError('CATALOG_MEMBER_UNUSED_PLAN_EXPIRED')
                session.add(JobCursor(job_name=self.job_name(plan),cursor_key=plan['target']['code'],cursor_value={'plan':plan},updated_at=now))
                session.flush()
            else:self._root(session,plan)
            key=catalog_run_key(plan,document)
            details={'schemaVersion':1,'plan':plan,'document':document,'phase':'reserved','rawId':None,'normalized':None}
            session.add(JobRun(id=key,job_name=self.job_name(plan),business_key=self.business_key(plan,document),
                status='running',started_at=now,request_count=1,row_count=0,details=details))
            session.add(IngestionBatch(id=key,source='official',dataset='enterprise-official-members',mode='live',
                status='running',started_at=now,request_count=1,row_count=0,rejected_count=0))
        return key
