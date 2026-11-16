"""官方成员家族共享上海日计数；未知预留也占额，不复制每目标日预算。"""
from datetime import datetime,time,timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import or_,select,text

from app.db.models.ops import JobRun,JobCursor
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant

LEGACY_JOB='enterprise-official-members-v1'
CATALOG_JOB_PREFIX='enterprise-official-catalog-v1:'
DISCOVERY_JOB_PREFIX=CATALOG_JOB_PREFIX+'discovery:'
GRAPH_JOB_PREFIX=CATALOG_JOB_PREFIX+'graph:'
SAMPLE_JOB_PREFIX=CATALOG_JOB_PREFIX+'sample:'
RECOVERY_JOB_PREFIX=CATALOG_JOB_PREFIX+'sample-recovery:'
LOCK_NAME='enterprise-official-member-family-budget-v1'


def family_lock(session):
    key=int.from_bytes(sha256(LOCK_NAME.encode()).digest()[:8],'big',signed=True)
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':key})


def daily_budget(session,now,limit):
    now=instant(now)
    if limit is not None and (type(limit) is not int or not 1<=limit<=4):raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID')
    local=now.astimezone(ZoneInfo('Asia/Shanghai'))
    start=datetime.combine(local.date(),time(),local.tzinfo);end=start+timedelta(days=1)
    rows=session.scalars(select(JobRun).where(or_(
        JobRun.job_name==LEGACY_JOB,JobRun.job_name.like(CATALOG_JOB_PREFIX+'%')))).all()
    used=0
    for row in rows:
        job,at,count,details=row.job_name,row.started_at,row.request_count,row.details
        if type(count) is not int or count!=1 or utc(at)>now:raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID')
        # 全家族共用计数，不能只相信另一入口留下的数字；复用现有根/收据校验。
        try:
            if job==LEGACY_JOB:
                from app.macro.official_member_store import OfficialMemberStore
                store=OfficialMemberStore(None)
            elif job.startswith(SAMPLE_JOB_PREFIX):
                from app.macro.issuer_sample_store import IssuerSampleStore
                store=IssuerSampleStore(None)
            elif job.startswith(RECOVERY_JOB_PREFIX):
                from app.macro.issuer_recovery_store import IssuerRecoveryStore
                store=IssuerRecoveryStore(None)
            elif job.startswith(DISCOVERY_JOB_PREFIX):
                from app.macro.fund_contract_store import FundContractStore
                store=FundContractStore(None)
            elif job.startswith(GRAPH_JOB_PREFIX):
                from app.macro.fund_member_graph_store import FundMemberGraphStore
                store=FundMemberGraphStore(None)
            else:
                from app.macro.catalog_member_store import CatalogMemberStore
                store=CatalogMemberStore(None)
            store._run(session,row)
        except Exception:raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID') from None
        if job.startswith((DISCOVERY_JOB_PREFIX,SAMPLE_JOB_PREFIX,RECOVERY_JOB_PREFIX)):
            prefix=next(p for p in (DISCOVERY_JOB_PREFIX,SAMPLE_JOB_PREFIX,RECOVERY_JOB_PREFIX) if job.startswith(p))
            root=session.scalar(select(JobCursor).where(JobCursor.job_name==job,JobCursor.cursor_key==job.removeprefix(prefix)))
            if (root is None or type(root.cursor_value) is not dict or type(root.cursor_value.get('plan')) is not dict
                or type(details) is not dict or root.cursor_value!={'plan':details.get('plan')}
                or utc(root.updated_at)!=utc(at)
                or root.cursor_value['plan'].get('firstBusinessDate')!=utc(at).astimezone(local.tzinfo).date().isoformat()):
                raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID')
        if job.startswith(GRAPH_JOB_PREFIX):
            root=session.scalar(select(JobCursor).where(JobCursor.job_name==job,JobCursor.cursor_key==job.removeprefix(GRAPH_JOB_PREFIX)))
            try:
                p=details['plan'];intent=p['intent'];started=instant(datetime.fromisoformat(p['reservedAt']))
                if (root is None or root.cursor_value!={'intent':intent} or started!=utc(at) or utc(at)<utc(root.updated_at)
                    or intent['firstBusinessDate']!=utc(root.updated_at).astimezone(local.tzinfo).date().isoformat()
                    or (p['document']['kind']=='csi-members' and utc(at)!=utc(root.updated_at))):raise ValueError()
            except Exception:raise ValueError('OFFICIAL_MEMBER_BUDGET_INVALID') from None
        if start<=utc(at)<end:used+=1
    return {'businessDate':local.date().isoformat(),'limit':limit,'used':used,'remaining':None if limit is None else max(0,limit-used),
            'basis':'committed-official-family-attempts-shanghai-day'}
