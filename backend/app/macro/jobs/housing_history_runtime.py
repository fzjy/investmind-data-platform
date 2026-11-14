"""独立有界历史入口；只用实际时钟和根配置，不自动发布或重试。"""
import asyncio
from datetime import datetime,timezone
import time
from sqlalchemy import select,func

from app.db.models.ops import JobCursor,JobRun
from app.macro.jobs.macro_extension_runtime import LOCK_KEY
from app.macro.extensions.config import load_extension_config
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.ingest import utc
from app.macro.housing_history_plan import DIRECTORY_VERSION,REPORT_VERSION,build_directory_plan,build_report_plan,validate_history_plan
from app.macro.official_macro_store import OfficialMacroStore,job_name

SOURCE_CONFLICTS={'HISTORY_REPORT_AMBIGUOUS','HISTORY_DIRECTORY_CHANGED'}


def report_plan_or_conflict(plan,documents):
    """已验证Raw之间的来源冲突与损坏账本分开，后者继续抛错。"""
    try:return build_report_plan(plan,documents),None
    except ValueError as error:
        if str(error) not in SOURCE_CONFLICTS:raise
        return None,str(error)


def completed_documents(factory,plan,now):
    store=OfficialMacroStore(factory);state=store.inspect(plan,now);result=[]
    with factory() as session:
        for entry in state['documents']:
            if entry['phase']!='completed':break
            row=session.get(JobRun,entry['runId']);d=store._run(session,row);_,retrieved=store._raw(session,row)
            result.append(dict(runId=row.id,documentId=d['document']['id'],request=d['request'],
                retrievedAt=retrieved.isoformat(),completedAt=utc(row.finished_at).isoformat(),normalized=d['normalized']))
            if 'recovery' in d:result[-1]['recovery']=d['recovery']
    return result


def directory_plan(factory,config,now):
    policy=config.historyBackfills['tier1Housing'];name=DIRECTORY_VERSION+':tier1Housing'
    with factory() as session:
        roots=list(session.scalars(select(JobCursor).where(JobCursor.job_name==name)))
    if len(roots)>1:raise ValueError('HISTORY_ROOT_AMBIGUOUS')
    if roots:
        root=roots[0]
        if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('HISTORY_ROOT_INVALID')
        plan=root.cursor_value['plan'];validate_history_plan(plan)
        if root.cursor_key!=plan['scheduledAt'] or plan['groupPolicy']!=policy.model_dump(exclude={'collectionEnabled'}):raise ValueError('HISTORY_POLICY_CHANGED')
        return plan
    from app.macro.official_macro_view import load_official_macro_view
    current=load_official_macro_view(factory,now);known=[]
    if current is not None:
        series=current['groups']['tier1Housing']['series']
        sets=[{p['month'][:7] for p in s['points'] if p['value'] is not None} for s in series]
        if len(sets)==2:known=sorted(sets[0]&sets[1])
    normal=build_extension_plan('tier1Housing',config,now)
    known=[m for m in known if normal['startPeriod']<=m+'-01'<=normal['endPeriod']]
    return build_directory_plan(policy.model_dump(),now,normal['startPeriod'],normal['endPeriod'],known)


def execute_housing_history(factory,config,*,clock=None,transport=None,dry_run=False,rate_limiter=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    out=dict(readOnly=True,supplierHttp=0,supplierHttpCountBasis='transport-attempt-upper-bound',insertedRows=0,publicationRequested=False,physicalBudgetEnforced=True)
    try:
        policy=config.historyBackfills.get('tier1Housing')
        if policy is None or not policy.collectionEnabled:return {**out,'status':'disabled'}
        plan=directory_plan(factory,config,clock());store=OfficialMacroStore(factory)
        original=plan;state=store.inspect(plan,clock())
        if dry_run:return {**out,'status':'preview','directoryPlan':plan,'execution':state,'maxHttpTotal':policy.maxHttpTotal}
        while True:
            state=store.inspect(plan,clock())
            if state['status']=='blocked':return {**out,'status':'blocked','errorCode':'HISTORY_ATTEMPT_BLOCKED','phase':plan['normalizerVersion']}
            for entry in state['documents']:
                if entry['phase']=='completed':continue
                if entry['phase']=='new':
                    if out['supplierHttp']>=policy.maxHttpPerRun:return {**out,'status':'partial','phase':plan['normalizerVersion']}
                    if rate_limiter is not None:rate_limiter.acquire()
                    current=load_extension_config().historyBackfills.get('tier1Housing')
                    if current is None or not current.collectionEnabled or current.model_dump(exclude={'collectionEnabled'})!=original['groupPolicy']:
                        return {**out,'status':'blocked','errorCode':'HISTORY_COLLECTION_CHANGED'}
                    from app.macro.housing_history_recovery import reserved_total
                    total=reserved_total(factory)
                    if total>=policy.maxHttpTotal:return {**out,'status':'blocked','errorCode':'HISTORY_TOTAL_BUDGET_EXHAUSTED'}
                    out['readOnly']=False
                    try:key=store.reserve(plan,entry['document'],clock())
                    except Exception:return {**out,'status':'blocked','errorCode':'HISTORY_RESERVATION_UNCERTAIN','reservationUncertain':True}
                    out['supplierHttp']+=1
                    try:
                        body=bytes(asyncio.run(ExtensionClient('public-official-document',transport=transport)._read(plan,'GET',entry['document']['url'])))
                    except Exception as error:
                        from app.macro.extensions.store import FAILURE_CODES
                        code=str(error) if str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                        store.fail(key,code,clock())
                        return {**out,'status':'blocked','errorCode':code,'runId':key}
                    store.receive(key,body,clock())
                else:key=entry['runId']
                out['readOnly']=False
                try:out['insertedRows']+=store.replay(key,clock()).get('insertedRows',0)
                except Exception:return {**out,'status':'replay_pending','errorCode':'HISTORY_NORMALIZATION_PENDING','runId':key}
            state=store.inspect(plan,clock())
            if state['status']!='completed':return {**out,'status':state['status']}
            if plan['normalizerVersion']==REPORT_VERSION:return {**out,'status':'completed','planFingerprint':plan['fingerprint']}
            plan,conflict=report_plan_or_conflict(original,completed_documents(factory,original,clock()))
            if conflict is not None:return {**out,'status':'blocked','errorCode':conflict,'sourceConflict':True}
            if plan is None:return {**out,'status':'completed','noNewReports':True}
    except Exception:return {**out,'status':'blocked','errorCode':'HISTORY_PLAN_OR_LEDGER_INVALID'}


def load_history_group(factory,as_of):
    """仅发布整个报告组已完成、Raw/Core与目录全闭合的历史，不隐藏不合格证明。"""
    with factory() as session:
        roots=list(session.scalars(select(JobCursor).where(JobCursor.job_name==DIRECTORY_VERSION+':tier1Housing')))
    if not roots:return None
    if len(roots)!=1:raise ValueError('HISTORY_ROOT_AMBIGUOUS')
    plan=roots[0].cursor_value['plan'];validate_history_plan(plan)
    if datetime.fromisoformat(plan['scheduledAt'])>as_of:return None
    # 尚在进行的目录/报告不进入发布，已有周历史仍照常展示。
    with factory() as session:
        store=OfficialMacroStore(factory)
        rows=[store.effective_run(session,r,as_of) for r in session.scalars(select(JobRun).where(JobRun.job_name==job_name(plan)))]
    if len(rows)!=len(plan['documents']) or any(r.status!='completed' or utc(r.finished_at)>as_of for r in rows):return None
    documents=completed_documents(factory,plan,as_of);report,conflict=report_plan_or_conflict(plan,documents)
    if conflict is not None:return None
    if report is None:return None
    with factory() as session:
        rows=[store.effective_run(session,r,as_of) for r in session.scalars(select(JobRun).where(JobRun.job_name==job_name(report)))]
    if len(rows)!=len(report['documents']) or any(r.status!='completed' or utc(r.finished_at)>as_of for r in rows):return None
    from app.macro.official_macro_view import build_official_macro_view,OFFICIAL_MACRO_GROUPS
    docs=completed_documents(factory,report,as_of)
    raw=dict(plan=report,documents=docs,latestAttempt=dict(plan=report,status='completed',errorCode=None,
        startedAt=min(utc(r.started_at) for r in rows).isoformat(),finishedAt=max(utc(r.finished_at) for r in rows).isoformat()))
    groups={key:dict(plan=None,documents=[],latestAttempt=None) for key in OFFICIAL_MACRO_GROUPS};groups['tier1Housing']=raw
    from zoneinfo import ZoneInfo
    return build_official_macro_view(dict(analysisDate=as_of.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),groups=groups))['groups']['tier1Housing']


def run_housing_history(settings,*,dry_run=False,recovery_parent_id=None):
    config=load_extension_config()
    if settings.data_backend!='database' or not settings.database_url:raise ValueError('HISTORY_DATABASE_REQUIRED')
    if dry_run:
        from app.shared.readonly_database import open_readonly_factory
        with open_readonly_factory(settings.database_url) as factory:
            if recovery_parent_id is not None:
                from app.macro.housing_history_recovery import execute_recovery
                return execute_recovery(factory,config,recovery_parent_id,dry_run=True)
            return execute_housing_history(factory,config,dry_run=True)
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine);acquired=False
    try:
        acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:return dict(status='locked',readOnly=True,supplierHttp=0,publicationRequested=False)
        config=load_extension_config();policy=config.historyBackfills.get('tier1Housing')
        if policy is None or not policy.collectionEnabled:return dict(status='disabled',readOnly=True,supplierHttp=0)
        base=RequestRateLimiter(policy.requestsPerMinute)
        class DurableRate:
            def acquire(self):
                from app.macro.housing_history_recovery import JOB as RECOVERY_JOB
                with factory() as s:
                    last=s.scalar(select(func.max(JobRun.started_at)).where(JobRun.job_name.in_([DIRECTORY_VERSION+':tier1Housing',REPORT_VERSION+':tier1Housing',RECOVERY_JOB])))
                if last is not None:
                    delay=60/policy.requestsPerMinute-(datetime.now(timezone.utc)-utc(last)).total_seconds()
                    if delay>0:time.sleep(delay)
                base.acquire()
        if recovery_parent_id is not None:
            from app.macro.housing_history_recovery import execute_recovery
            return execute_recovery(factory,config,recovery_parent_id,rate_limiter=DurableRate())
        return execute_housing_history(factory,config,rate_limiter=DurableRate())
    finally:
        if acquired:lock.release(LOCK_KEY)
        engine.dispose()
