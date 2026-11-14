"""来源核查独立静态根；共用官方家族锁/日额度及严格Raw收据。"""
from sqlalchemy import select
from zoneinfo import ZoneInfo

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant
from app.macro.official_member_store import OfficialMemberStore
from app.macro.official_member_budget import family_lock
from app.macro.fund_contract_discovery import PREFIX,run_key,validate_discovery_plan,parse_fund_contract


class FundContractStore(OfficialMemberStore):
    def validate_plan(self,plan):return validate_discovery_plan(plan)
    def job_name(self,plan):return PREFIX+plan['document']['fundCode']
    def business_key(self,plan,document):return 'fund-contract:'+document['fundCode']
    def documents(self,plan):return [plan['document']]
    def run_key(self,plan,document):return run_key(plan)
    def max_response_bytes(self,plan):return plan['limits']['maxResponseBytes']
    def dataset(self,plan):return 'enterprise-fund-contracts'
    def api_name(self,plan,document):return 'official-fund-contract'
    def normalize(self,plan,document,body):return parse_fund_contract(body,document['fundCode'],plan['target']['sourceIndexNameHint'],document=document)

    def _root(self,session,plan):
        self.validate_plan(plan)
        root=session.scalar(select(JobCursor).where(JobCursor.job_name==self.job_name(plan),JobCursor.cursor_key==plan['document']['fundCode']))
        if root is None or root.cursor_value!={'plan':plan}:raise ValueError('FUND_CONTRACT_DISCOVERY_ROOT_INVALID')
        return root

    def _run(self,session,row):
        value=super()._run(session,row);root=self._root(session,value['plan']);at=utc(row.started_at)
        if (utc(root.updated_at)!=at
            or at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()!=value['plan']['firstBusinessDate']):
            raise ValueError('FUND_CONTRACT_DISCOVERY_ROOT_TIME_INVALID')
        return value

    def inspect(self,plan,now):
        now=instant(now);self.validate_plan(plan)
        day=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if plan['firstBusinessDate']>day:raise ValueError('FUND_CONTRACT_DISCOVERY_CLOCK_REGRESSION')
        with self.factory() as session:
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==self.job_name(plan))).all()
            rows=session.scalars(select(JobRun).where(JobRun.job_name==self.job_name(plan))).all()
            if len(roots)>1 or len(rows)>1 or bool(roots)!=bool(rows):raise ValueError('FUND_CONTRACT_DISCOVERY_ROOT_INVALID')
            chosen=plan;phase='new';normalized=None;error=None;key=None
            if roots:
                root=roots[0]
                if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('FUND_CONTRACT_DISCOVERY_ROOT_INVALID')
                chosen=root.cursor_value['plan'];self._root(session,chosen)
                if ({k:v for k,v in chosen.items() if k not in ('firstBusinessDate','fingerprint')}
                    !={k:v for k,v in plan.items() if k not in ('firstBusinessDate','fingerprint')}
                    or chosen['firstBusinessDate']>day or utc(root.updated_at)>now):raise ValueError('FUND_CONTRACT_DISCOVERY_POLICY_CHANGED')
                row=rows[0];d=self._run(session,row)
                if (d['plan']!=chosen or utc(row.started_at)>now or row.finished_at is not None and utc(row.finished_at)>now
                    or d['rawId'] is not None and self._raw(session,row)[1]>now):raise ValueError('FUND_CONTRACT_DISCOVERY_CLOCK_REGRESSION')
                phase=d['phase'];normalized=d['normalized'];error=row.error_message;key=row.id
            return {'phase':phase,'plan':chosen,'runId':key,'normalized':normalized,'errorCode':error,
                'proofReady':False,'dailyBudget':self.budget(session,now,chosen['limits']['maxHttpPerDay'])}

    def reserve(self,plan,now):
        now=instant(now);doc=self.validate_plan(plan)
        with self.factory.begin() as session:
            family_lock(session);state=self.inspect(plan,now)
            if state['phase']!='new':raise ValueError('FUND_CONTRACT_DISCOVERY_RESERVATION_BLOCKED')
            if plan['firstBusinessDate']!=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
                raise ValueError('FUND_CONTRACT_DISCOVERY_UNUSED_PLAN_EXPIRED')
            if self.budget(session,now,plan['limits']['maxHttpPerDay'])['remaining']==0:
                raise ValueError('OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED')
            key=self.run_key(plan,plan['document'])
            session.add(JobCursor(job_name=self.job_name(plan),cursor_key=doc.fundCode,cursor_value={'plan':plan},updated_at=now));session.flush()
            session.add(JobRun(id=key,job_name=self.job_name(plan),business_key=self.business_key(plan,plan['document']),
                status='running',started_at=now,request_count=1,row_count=0,
                details={'schemaVersion':1,'plan':plan,'document':plan['document'],'phase':'reserved','rawId':None,'normalized':None}))
            session.add(IngestionBatch(id=key,source='official',dataset=self.dataset(plan),mode='live',status='running',
                started_at=now,request_count=1,row_count=0,rejected_count=0))
        return key
