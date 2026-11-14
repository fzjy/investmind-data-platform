"""官方成员的独立有界正常入口，不发布、不调用Tushare或启动scheduler。"""
import asyncio
from datetime import datetime,timezone

from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import FAILURE_CODES
from app.macro.official_member_store import OfficialMemberStore,build_plan

LOCK_KEY='enterprise-official-members-execution-v1'


def execute_official_members(factory,catalog,*,clock=None,transport=None,rate_limiter=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'publicationRequested':False,
        'physicalBudgetEnforced':True,'supplierHttpCountBasis':'transport-attempt-upper-bound','sources':[]}
    try:
        policy=catalog.official_policy
        if policy is None or not policy.collectionEnabled:return {**result,'status':'disabled'}
        plan=build_plan(policy,catalog.fingerprint,clock());store=OfficialMemberStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled)
        state=store.inspect(plan,clock());plan=state['plan'];result['planFingerprint']=plan['fingerprint']
        if state['status']=='blocked':
            entry=next(e for e in state['documents'] if e['phase'] in ('reserved','failed'))
            return {**result,'status':'blocked','errorCode':entry['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN'}
        for entry in state['documents']:
            doc,key=entry['document'],entry['runId']
            if entry['phase']=='completed':continue
            if entry['phase']=='new':
                if result['requestCount']>=plan['policy']['maxHttpPerRun']:break
                if rate_limiter is not None:rate_limiter.acquire()
                if before_send is not None:before_send()
                current=load_enterprise_target_catalog()
                if (current.fingerprint!=plan['catalogFingerprint'] or current.official_policy is None
                    or current.official_policy.dailyBudgetEnabled!=policy.dailyBudgetEnabled
                    or not current.official_policy.collectionEnabled or current.official_policy.fingerprint!=plan['policyHash']):
                    return {**result,'status':'blocked','errorCode':'OFFICIAL_MEMBER_COLLECTION_CHANGED'}
                result['readOnly']=False
                try:key=store.reserve(plan,doc,clock())
                except Exception:
                    return {**result,'status':'blocked','errorCode':'OFFICIAL_MEMBER_RESERVATION_UNCERTAIN','reservationUncertain':True,'requestCount':result['requestCount']+1}
                result['requestCount']+=1
                current=load_enterprise_target_catalog()
                if current.fingerprint!=catalog.fingerprint or current.official_policy!=policy:
                    return {**result,'status':'blocked','errorCode':'OFFICIAL_MEMBER_COLLECTION_CHANGED',
                        'dailyBudget':store.inspect(plan,clock())['dailyBudget']}
                result['supplierHttp']+=1
                try:
                    limits={k:plan['policy'][k] for k in ('requestTimeoutSeconds','maxResponseBytes')}
                    reader=ExtensionClient('public-document',transport=transport)
                    body=bytes(asyncio.run(reader._read({'limits':limits},'GET',doc['url'])))
                    result['responseCount']+=1
                except Exception as error:
                    code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                    try:store.fail(key,code,clock())
                    except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                    return {**result,'status':'blocked','errorCode':code}
                try:store.receive(key,body,clock())
                except Exception:return {**result,'status':'blocked','errorCode':'OFFICIAL_MEMBER_RAW_RECEIVE_PENDING'}
            result['readOnly']=False
            try:receipt=store.replay(key,clock())
            except Exception:return {**result,'status':'replay_pending','errorCode':'OFFICIAL_MEMBER_NORMALIZATION_PENDING','runId':key}
            result['sources'].append({'documentId':doc['id'],**receipt})
        final=store.inspect(plan,clock())
        return {**result,'status':final['status'],'reservedCount':final['reservedCount'],
            'dailyBudget':final['dailyBudget'],'dailyBudgetCheckedAt':clock().isoformat()}
    except Exception:return {**result,'status':'blocked','errorCode':'OFFICIAL_MEMBER_EXECUTION_UNCERTAIN' if not result['readOnly'] else 'OFFICIAL_MEMBER_EXECUTION_INVALID'}


def run_official_members(settings,*,dry_run=False,transport=None,clock=None):
    from sqlalchemy.engine import make_url
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    from app.shared.readonly_database import open_readonly_factory
    catalog=load_enterprise_target_catalog();policy=catalog.official_policy
    base={'readOnly':True,'supplierHttp':0,'publicationRequested':False}
    if settings.data_backend!='database' or not settings.database_url:
        return {**base,'status':'blocked','errorCode':'OFFICIAL_MEMBER_DATABASE_REQUIRED'}
    url=make_url(settings.database_url)
    if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':
        return {**base,'status':'blocked','errorCode':'OFFICIAL_MEMBER_POSTGRES_REQUIRED'}
    if policy is None:return {**base,'status':'disabled'}
    clock=clock or (lambda:datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            plan=build_plan(policy,catalog.fingerprint,clock());state=OfficialMemberStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled).inspect(plan,clock())
            return {**base,'status':'preview','collectionEnabled':policy.collectionEnabled,'executionState':state}
    if not policy.collectionEnabled:return {**base,'status':'disabled'}
    engine,lock,acquired=None,None,False;report={}
    try:
        engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine)
        acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:report.update({**base,'status':'locked'});return report
        current=load_enterprise_target_catalog()
        if current.fingerprint!=catalog.fingerprint or current.official_policy!=policy:
            report.update({**base,'status':'blocked','errorCode':'OFFICIAL_MEMBER_COLLECTION_CHANGED'});return report
        report.update(execute_official_members(factory,current,clock=clock,transport=transport,
            rate_limiter=RequestRateLimiter(policy.requestsPerMinute)))
        return report
    except Exception:
        # 执行组内部捕获所有发送后异常，保留已知实际计数；此前异常均未调用传输。
        report.update({**base,'status':'blocked','errorCode':'OFFICIAL_MEMBER_RUNTIME_FAILED'});return report
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),
            ('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:report.setdefault('cleanupErrors',[]).append(stage)
