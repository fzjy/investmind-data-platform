"""其余目录正常入口；逐目标恢复，全部目标共享单次/上海日预算。"""
import asyncio
from datetime import datetime,timezone

from app.macro.catalog_member_policy import derived_policy
from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import FAILURE_CODES


def current_policy_matches(catalog,plan,daily_budget_enabled=None):
    try:
        if daily_budget_enabled is not None and (catalog.official_policy is None
            or catalog.official_policy.dailyBudgetEnabled!=daily_budget_enabled):return False
        policy=catalog.catalog_member_policy
        if policy is None or not policy.collectionEnabled or catalog.fingerprint!=plan['catalogFingerprint']:return False
        target=next(t for t in catalog.targets if t.code==plan['target']['code'])
        scope=next(t for t in policy.targets if t.code==target.code)
        expected={'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint}
        return expected==plan['target'] and derived_policy(catalog.official_policy,target,scope).fingerprint==plan['policyHash']
    except (ValueError,TypeError,AttributeError,StopIteration):return False


def execute_catalog_members(factory,catalog,*,clock=None,transport=None,rate_limiter=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'publicationRequested':False,
            'physicalBudgetEnforced':True,'supplierHttpCountBasis':'transport-attempt-upper-bound','groups':[]}
    if catalog.catalog_member_policy is None or not catalog.catalog_member_policy.collectionEnabled:
        return {**result,'status':'disabled','sourceCoverageReady':False,'configuredTargetsCompleted':False}
    store=CatalogMemberStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled);limits=catalog.official_policy
    for scope in catalog.catalog_member_policy.targets:
        group={'targetCode':scope.code,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'sources':[]}
        if not scope.documents:
            result['groups'].append({**group,'status':'source_pending','errorCode':'CATALOG_MEMBER_SOURCE_CONTRACT_PENDING'})
            continue
        try:
            plan=build_catalog_member_plan(catalog,scope.code,clock());state=store.inspect(plan,clock());plan=state['plan']
            group.update(planFingerprint=plan['fingerprint'],firstBusinessDate=plan['businessDate'],dailyBudget=state['dailyBudget'])
            if state['status']=='blocked':
                bad=next(e for e in state['documents'] if e['phase'] in ('reserved','failed'))
                group.update(status='blocked',errorCode=bad['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN')
            else:
                group['status']='completed'
                for entry in state['documents']:
                    doc,key=entry['document'],entry['runId']
                    if entry['phase']=='completed':continue
                    if entry['phase']=='new':
                        fresh=store.inspect(plan,clock())
                        if result['requestCount']+group['requestCount']>=limits.maxHttpPerRun or fresh['dailyBudget']['remaining']==0:
                            group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING');break
                        if rate_limiter is not None:rate_limiter.acquire()
                        if before_send is not None:before_send()
                        if not current_policy_matches(load_enterprise_target_catalog(),plan,catalog.official_policy.dailyBudgetEnabled):
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED');break
                        group['readOnly']=False
                        try:key=store.reserve(plan,doc,clock())
                        except Exception as error:
                            if isinstance(error,ValueError) and str(error)=='OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED':
                                group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
                            else:
                                group.update(status='blocked',errorCode='OFFICIAL_MEMBER_RESERVATION_UNCERTAIN',reservationUncertain=True,
                                    requestCount=group['requestCount']+1)
                            break
                        group['requestCount']+=1
                        if not current_policy_matches(load_enterprise_target_catalog(),plan,catalog.official_policy.dailyBudgetEnabled):
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED');break
                        group['supplierHttp']+=1
                        try:
                            reader=ExtensionClient('public-official-document',transport=transport)
                            bounds={k:plan['policy'][k] for k in ('requestTimeoutSeconds','maxResponseBytes')}
                            body=bytes(asyncio.run(reader._read({'limits':bounds},'GET',doc['url'])))
                            group['responseCount']+=1
                        except Exception as error:
                            code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                            try:store.fail(key,code,clock())
                            except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                            group.update(status='blocked',errorCode=code);break
                        try:store.receive(key,body,clock())
                        except Exception:
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_RAW_RECEIVE_PENDING');break
                    try:
                        replay=store.replay(key,clock());group['readOnly']=group['readOnly'] and replay['readOnly']
                        group['sources'].append({'documentId':doc['id'],'runId':key,'phase':'completed'})
                    except Exception:
                        group.update(status='replay_pending',errorCode='OFFICIAL_MEMBER_NORMALIZE_PENDING');break
        except Exception:
            group.update(status='blocked',errorCode='CATALOG_MEMBER_EXECUTION_INVALID')
        try:
            checked=clock()
            with factory() as session:group['dailyBudget']=store.budget(session,checked,catalog.official_policy.maxHttpPerDay)
            group['dailyBudgetCheckedAt']=checked.isoformat()
        except Exception:group.update(status='blocked',errorCode='OFFICIAL_MEMBER_BUDGET_INVALID')
        result['groups'].append(group)
        for key in ('supplierHttp','requestCount','responseCount'):result[key]+=group[key]
        result['readOnly']=result['readOnly'] and group['readOnly']
    states=[g['status'] for g in result['groups']]
    configured=[g['status'] for g in result['groups'] if g['status']!='source_pending']
    result.update(configuredTargetsCompleted=bool(configured) and all(s=='completed' for s in configured),
        sourceCoverageReady=bool(states) and all(s=='completed' for s in states),
        status='blocked' if any(s in ('blocked','replay_pending') for s in states) else
            'partial' if any(s!='completed' for s in states) else 'completed')
    return result


def preview_catalog_members(factory,catalog,now):
    from app.macro.official_member_budget import daily_budget
    groups=[]
    for scope in catalog.catalog_member_policy.targets:
        group={'targetCode':scope.code,'sourceContractReady':bool(scope.documents),'executionState':None}
        if scope.documents:
            p=build_catalog_member_plan(catalog,scope.code,now)
            group['executionState']=CatalogMemberStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled).inspect(p,now)
        else:group.update(status='source_pending',errorCode='CATALOG_MEMBER_SOURCE_CONTRACT_PENDING')
        groups.append(group)
    with factory() as session:budget=daily_budget(session,now,catalog.official_policy.maxHttpPerDay if catalog.official_policy.dailyBudgetEnabled else None)
    return {'status':'preview','readOnly':True,'supplierHttp':0,'publicationRequested':False,
            'collectionEnabled':catalog.catalog_member_policy.collectionEnabled,'groups':groups,'dailyBudget':budget}


def run_catalog_members(settings,*,dry_run=False,transport=None,clock=None,before_send=None):
    from sqlalchemy.engine import make_url
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.macro.jobs.official_member_runtime import LOCK_KEY
    from app.macro.extensions.store import execution_schema
    from app.shared.readonly_database import open_readonly_factory
    base={'readOnly':True,'supplierHttp':0,'publicationRequested':False}
    catalog=load_enterprise_target_catalog();policy=catalog.catalog_member_policy
    if policy is None or not dry_run and not policy.collectionEnabled:return {**base,'status':'disabled'}
    if settings.data_backend!='database' or not settings.database_url:return {**base,'status':'blocked','errorCode':'CATALOG_MEMBER_DATABASE_REQUIRED'}
    url=make_url(settings.database_url)
    if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':
        return {**base,'status':'blocked','errorCode':'CATALOG_MEMBER_POSTGRES_REQUIRED'}
    clock=clock or (lambda:datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:return preview_catalog_members(factory,catalog,clock())
    engine,lock,acquired=None,None,False;result={}
    try:
        engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine)
        acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:result.update({**base,'status':'locked'});return result
        problems=execution_schema(factory)
        if problems:result.update({**base,'status':'blocked','errorCode':'CATALOG_MEMBER_SCHEMA_REQUIRED','problems':problems});return result
        current=load_enterprise_target_catalog()
        if current.catalog_member_policy!=policy or current.official_policy!=catalog.official_policy or current.fingerprint!=catalog.fingerprint:
            result.update({**base,'status':'blocked','errorCode':'OFFICIAL_MEMBER_COLLECTION_CHANGED'});return result
        result.update(execute_catalog_members(factory,current,clock=clock,transport=transport,before_send=before_send,
            rate_limiter=RequestRateLimiter(current.official_policy.requestsPerMinute)))
        return result
    except Exception:
        result.update({**base,'status':'blocked','errorCode':'CATALOG_MEMBER_RUNTIME_FAILED'});return result
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:result.setdefault('cleanupErrors',[]).append(stage)
