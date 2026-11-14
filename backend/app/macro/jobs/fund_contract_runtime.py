"""正常有界官方来源合同核查；共用官方家族额度，不产生完整成员或发布。"""
import asyncio
from datetime import datetime,timezone

from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.macro.fund_contract_discovery import BOUNDS,build_discovery_plan
from app.macro.fund_contract_store import FundContractStore
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import FAILURE_CODES


def current_policy_matches(catalog,plan,daily_budget_enabled=None):
    try:
        if daily_budget_enabled is not None and (catalog.official_policy is None
            or catalog.official_policy.dailyBudgetEnabled!=daily_budget_enabled):return False
        p=catalog.catalog_member_policy
        if p is None or not p.collectionEnabled or not p.discoveryEnabled or catalog.fingerprint!=plan['catalogFingerprint']:return False
        doc=next(d for d in p.discoveryDocuments if d.id==plan['document']['id'])
        target=next(t for t in catalog.targets if t.code==plan['target']['code'])
        expected={'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint}
        return (doc.model_dump()==plan['document'] and expected==plan['target']
            and {k:getattr(catalog.official_policy,k) for k in BOUNDS}==plan['limits'])
    except Exception:return False


def execute_fund_contracts(factory,catalog,*,clock=None,transport=None,rate_limiter=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'publicationRequested':False,
        'proofReady':False,'purpose':'source-contract-discovery-only','physicalBudgetEnforced':True,
        'supplierHttpCountBasis':'transport-attempt-upper-bound','groups':[]}
    policy=catalog.catalog_member_policy
    if policy is None or not policy.collectionEnabled or not policy.discoveryEnabled:return {**result,'status':'disabled'}
    store=FundContractStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled)
    for doc in policy.discoveryDocuments:
        group={'documentId':doc.id,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'proofReady':False}
        try:
            plan=build_discovery_plan(catalog,doc.id,clock());state=store.inspect(plan,clock());plan=state['plan'];key=state['runId']
            group.update(planFingerprint=plan['fingerprint'],dailyBudget=state['dailyBudget'])
            phase=state['phase']
            if phase in ('reserved','failed'):
                group.update(status='blocked',errorCode=state['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN')
            elif phase=='completed':group.update(status='completed',normalized=state['normalized'])
            else:
                if phase=='new':
                    if result['requestCount']>=catalog.official_policy.maxHttpPerRun or state['dailyBudget']['remaining']==0:
                        group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
                    else:
                        if rate_limiter is not None:rate_limiter.acquire()
                        if before_send is not None:before_send()
                        if not current_policy_matches(load_enterprise_target_catalog(),plan,catalog.official_policy.dailyBudgetEnabled):
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED')
                        else:
                            group['readOnly']=False
                            try:key=store.reserve(plan,clock())
                            except Exception as error:
                                if isinstance(error,ValueError) and str(error)=='OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED':
                                    group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
                                else:group.update(status='blocked',errorCode='FUND_CONTRACT_DISCOVERY_RESERVATION_UNCERTAIN',requestCount=1)
                            else:
                                group.update(requestCount=1)
                                if not current_policy_matches(load_enterprise_target_catalog(),plan,catalog.official_policy.dailyBudgetEnabled):
                                    group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED')
                                else:
                                    group['supplierHttp']=1
                                    try:
                                        from app.macro.fsfund_contracts import official_transport
                                        reader=ExtensionClient('public-official-document',transport=official_transport(plan['document'],transport))
                                        bounds={k:plan['limits'][k] for k in ('requestTimeoutSeconds','maxResponseBytes')}
                                        from app.macro.nffund_contracts import request_options
                                        method,options=request_options(plan['document'])
                                        body=bytes(asyncio.run(reader._read({'limits':bounds},method,plan['document']['url'],**options)))
                                        group['responseCount']=1
                                    except Exception as error:
                                        code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                                        try:store.fail(key,code,clock())
                                        except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                                        group.update(status='blocked',errorCode=code)
                                    else:
                                        try:store.receive(key,body,clock())
                                        except Exception:group.update(status='blocked',errorCode='FUND_CONTRACT_DISCOVERY_RAW_RECEIVE_PENDING')
                if 'status' not in group:
                    try:
                        replay=store.replay(key,clock());group['readOnly']=group['readOnly'] and replay['readOnly']
                        group.update(status='completed',normalized=store.inspect(plan,clock())['normalized'])
                    except Exception:group.update(status='replay_pending',errorCode='FUND_CONTRACT_DISCOVERY_NORMALIZE_PENDING')
        except Exception:group.update(status='blocked',errorCode='FUND_CONTRACT_DISCOVERY_EXECUTION_INVALID')
        try:
            checked=clock()
            with factory() as session:group['dailyBudget']=store.budget(session,checked,catalog.official_policy.maxHttpPerDay)
            group['dailyBudgetCheckedAt']=checked.isoformat()
        except Exception:group.update(status='blocked',errorCode='OFFICIAL_MEMBER_BUDGET_INVALID')
        result['groups'].append(group)
        for k in ('supplierHttp','requestCount','responseCount'):result[k]+=group[k]
        result['readOnly']=result['readOnly'] and group['readOnly']
    result['status']=('blocked' if any(g['status'] in ('blocked','replay_pending') for g in result['groups']) else
        'partial' if any(g['status']!='completed' for g in result['groups']) else 'completed')
    return result


def run_fund_contracts(settings,*,dry_run=False,transport=None,clock=None,before_send=None):
    from sqlalchemy.engine import make_url
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.macro.jobs.official_member_runtime import LOCK_KEY
    from app.macro.extensions.store import execution_schema
    from app.shared.readonly_database import open_readonly_factory
    base={'readOnly':True,'supplierHttp':0,'publicationRequested':False,'proofReady':False}
    catalog=load_enterprise_target_catalog();policy=catalog.catalog_member_policy
    if policy is None or not dry_run and (not policy.collectionEnabled or not policy.discoveryEnabled):return {**base,'status':'disabled'}
    if settings.data_backend!='database' or not settings.database_url:return {**base,'status':'blocked','errorCode':'FUND_CONTRACT_DATABASE_REQUIRED'}
    url=make_url(settings.database_url)
    if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':return {**base,'status':'blocked','errorCode':'FUND_CONTRACT_POSTGRES_REQUIRED'}
    clock=clock or (lambda:datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            groups=[{'documentId':d.id,'state':FundContractStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled).inspect(build_discovery_plan(catalog,d.id,clock()),clock())}
                for d in policy.discoveryDocuments]
            return {**base,'status':'preview','discoveryEnabled':policy.discoveryEnabled,'groups':groups}
    engine,lock,acquired=None,None,False;result={}
    try:
        engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine)
        acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:result.update({**base,'status':'locked'});return result
        if execution_schema(factory):result.update({**base,'status':'blocked','errorCode':'FUND_CONTRACT_SCHEMA_REQUIRED'});return result
        result.update(execute_fund_contracts(factory,catalog,clock=clock,transport=transport,before_send=before_send,
            rate_limiter=RequestRateLimiter(catalog.official_policy.requestsPerMinute)));return result
    except Exception:result.update({**base,'status':'blocked','errorCode':'FUND_CONTRACT_RUNTIME_FAILED'});return result
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:result.setdefault('cleanupErrors',[]).append(stage)
