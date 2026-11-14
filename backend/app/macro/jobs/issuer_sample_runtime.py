"""先完成已登记成员图，再按原共享额度采样发行人原件；不解析或发布。"""
import asyncio
from datetime import datetime,timezone

from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.macro.issuer_contract_samples import build_sample_plan
from app.macro.issuer_sample_store import IssuerSampleStore
from app.macro.fund_member_graph import build_fund_graph_intent
from app.macro.fund_member_graph_store import FundMemberGraphStore,load_source_contract_state
from app.macro.official_member_budget import daily_budget
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import FAILURE_CODES


def member_graph_prerequisites(factory,catalog,now):
    groups=[]
    for scope in catalog.catalog_member_policy.memberGraphs:
        group={'targetCode':scope.code,'witnessReady':False}
        try:
            parent=load_source_contract_state(factory,catalog,scope.code,now)
            state=FundMemberGraphStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled).inspect(build_fund_graph_intent(catalog,scope.code,parent['proof'],now),now) if parent['proof'] else None
            group.update(status=state['status'] if state else parent['phase'],witnessReady=state['witnessReady'] if state else False)
        except Exception:group.update(status='blocked',errorCode='ISSUER_SAMPLE_MEMBER_GRAPH_INVALID')
        groups.append(group)
    return groups


def current_policy_matches(catalog,plan,daily_budget_enabled=None):
    try:
        if daily_budget_enabled is not None and (catalog.official_policy is None
            or catalog.official_policy.dailyBudgetEnabled!=daily_budget_enabled):return False
        p=catalog.catalog_member_policy
        return (p is not None and p.collectionEnabled and p.sampleEnabled and catalog.fingerprint==plan['catalogFingerprint']
            and build_sample_plan(catalog,plan['document']['fundCode'],datetime.fromisoformat(plan['firstBusinessDate']+'T00:00:00+08:00'))==plan)
    except Exception:return False


def _make_plan(factory,catalog,code,now,recovery):
    if recovery:
        from app.macro.issuer_sample_recovery import build_recovery_plan
        return build_recovery_plan(factory,catalog,code,now)
    return build_sample_plan(catalog,code,now)


def _documents(catalog,recovery):
    documents=catalog.catalog_member_policy.sampleDocuments
    if not recovery:return documents
    registered={d.fundCode for d in catalog.catalog_member_policy.sampleRecoveries}
    return [d for d in documents if d.fundCode in registered]


def _store(factory,catalog,recovery):
    cls=IssuerSampleStore
    if recovery:
        from app.macro.issuer_recovery_store import IssuerRecoveryStore
        cls=IssuerRecoveryStore
    return cls(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled)


def _send_guard(factory,plan,now,result,daily_budget_enabled,recovery=False):
    try:
        current=load_enterprise_target_catalog()
        if recovery:
            policy=current.catalog_member_policy
            if (policy is None or not policy.collectionEnabled or not policy.sampleEnabled or current.official_policy is None
                or current.official_policy.dailyBudgetEnabled!=daily_budget_enabled):return 'OFFICIAL_MEMBER_COLLECTION_CHANGED'
            chosen=_make_plan(factory,current,plan['document']['fundCode'],now,True)
            ignored={'firstBusinessDate','fingerprint'}
            if {k:v for k,v in chosen.items() if k not in ignored}!={k:v for k,v in plan.items() if k not in ignored}:
                return 'OFFICIAL_MEMBER_COLLECTION_CHANGED'
        elif not current_policy_matches(current,plan,daily_budget_enabled):return 'OFFICIAL_MEMBER_COLLECTION_CHANGED'
        result['memberGraphPrerequisites']=member_graph_prerequisites(factory,current,now)
        if not all(g['witnessReady'] for g in result['memberGraphPrerequisites']):return 'ISSUER_SAMPLE_MEMBER_GRAPHS_PENDING'
    except Exception:return 'ISSUER_SAMPLE_SEND_GUARD_INVALID'
    return None


def execute_issuer_samples(factory,catalog,*,clock=None,transport=None,rate_limiter=None,before_send=None,recovery=False):
    if type(recovery) is not bool:raise ValueError('ISSUER_RECOVERY_MODE_INVALID')
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'publicationRequested':False,
        'proofReady':False,'purpose':'unparsed-issuer-originals-only','physicalBudgetEnforced':True,
        'supplierHttpCountBasis':'transport-attempt-upper-bound','groups':[]}
    policy=catalog.catalog_member_policy
    if policy is None or not policy.collectionEnabled or not policy.sampleEnabled:return {**result,'status':'disabled'}
    result['memberGraphPrerequisites']=member_graph_prerequisites(factory,catalog,clock())
    if recovery:result['purpose']='parent-linked-issuer-original-recovery'
    store=_store(factory,catalog,recovery);sends_halted=False
    for document in _documents(catalog,recovery):
        group={'fundCode':document.fundCode,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'proofReady':False}
        try:
            state=store.inspect(_make_plan(factory,catalog,document.fundCode,clock(),recovery),clock());plan=state['plan']
            group.update(planFingerprint=plan['fingerprint'],dailyBudget=state['dailyBudget'])
            if state['phase'] in ('failed','reserved'):
                group.update(status='blocked',errorCode=state['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN')
            elif state['phase']=='received':group.update(status='sample_received',sample=state['sample'])
            elif sends_halted:group.update(status='blocked',errorCode='ISSUER_SAMPLE_RUN_HALTED')
            elif not all(g['witnessReady'] for g in result['memberGraphPrerequisites']):
                group.update(status='member_graph_pending',errorCode='ISSUER_SAMPLE_MEMBER_GRAPHS_PENDING')
            elif result['requestCount']>=catalog.official_policy.maxHttpPerRun or state['dailyBudget']['remaining']==0:
                group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
            else:
                if rate_limiter is not None:rate_limiter.acquire()
                if before_send is not None:before_send()
                guard=_send_guard(factory,plan,clock(),result,catalog.official_policy.dailyBudgetEnabled,recovery)
                if guard:
                    group.update(status='member_graph_pending' if guard=='ISSUER_SAMPLE_MEMBER_GRAPHS_PENDING' else 'blocked',errorCode=guard)
                else:
                    group['readOnly']=False;key=None
                    try:key=store.reserve(plan,clock())
                    except Exception as error:
                        if isinstance(error,ValueError) and str(error)=='OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED':
                            group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
                        else:
                            group.update(status='blocked',errorCode='ISSUER_SAMPLE_RESERVATION_UNCERTAIN',requestCount=1)
                            sends_halted=True
                    if key is not None:
                        group['requestCount']=1
                        guard=_send_guard(factory,plan,clock(),result,catalog.official_policy.dailyBudgetEnabled,recovery)
                        if guard:
                            group.update(status='blocked',errorCode=guard);sends_halted=True
                        else:
                            group['supplierHttp']=1
                            try:
                                reader=ExtensionClient('public-official-document',transport=transport)
                                bounds={k:plan['limits'][k] for k in ('requestTimeoutSeconds','maxResponseBytes')}
                                options={'gzip_response_allowed':True} if recovery else {}
                                body=bytes(asyncio.run(reader._read({'limits':bounds},'GET',plan['document']['url'],**options)))
                                group['responseCount']=1
                                if not body:raise ExtensionError('EXTENSION_RESPONSE_INVALID')
                            except Exception as error:
                                code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                                try:store.fail(key,code,clock())
                                except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                                group.update(status='blocked',errorCode=code)
                            else:
                                try:
                                    store.receive(key,body,clock())
                                    group.update(status='sample_received',sample=store.inspect(plan,clock())['sample'])
                                except Exception:group.update(status='blocked',errorCode='ISSUER_SAMPLE_RAW_RECEIVE_PENDING')
        except Exception:group.update(status='blocked',errorCode='ISSUER_SAMPLE_EXECUTION_INVALID')
        try:
            at=clock()
            with factory() as session:group['dailyBudget']=daily_budget(session,at,catalog.official_policy.maxHttpPerDay if catalog.official_policy.dailyBudgetEnabled else None)
            group['dailyBudgetCheckedAt']=at.isoformat()
        except Exception:group.update(status='blocked',errorCode='ISSUER_SAMPLE_BUDGET_INVALID')
        result['groups'].append(group)
        for key in ('supplierHttp','requestCount','responseCount'):result[key]+=group[key]
        result['readOnly']=result['readOnly'] and group['readOnly']
    result['status']=('blocked' if any(g['status']=='blocked' for g in result['groups']) else
        'samples_received' if result['groups'] and all(g['status']=='sample_received' for g in result['groups']) else 'partial')
    result['sendsHalted']=sends_halted
    return result


def run_issuer_samples(settings,*,dry_run=False,transport=None,clock=None,before_send=None,recovery=False):
    if type(recovery) is not bool:raise ValueError('ISSUER_RECOVERY_MODE_INVALID')
    from sqlalchemy.engine import make_url
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.macro.jobs.official_member_runtime import LOCK_KEY
    from app.macro.extensions.store import execution_schema
    from app.shared.readonly_database import open_readonly_factory
    base={'readOnly':True,'supplierHttp':0,'publicationRequested':False,'proofReady':False}
    catalog=load_enterprise_target_catalog();policy=catalog.catalog_member_policy
    if policy is None or not dry_run and (not policy.collectionEnabled or not policy.sampleEnabled):return {**base,'status':'disabled'}
    if settings.data_backend!='database' or not settings.database_url:return {**base,'status':'blocked','errorCode':'ISSUER_SAMPLE_DATABASE_REQUIRED'}
    url=make_url(settings.database_url)
    if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':return {**base,'status':'blocked','errorCode':'ISSUER_SAMPLE_POSTGRES_REQUIRED'}
    clock=clock or (lambda:datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            if execution_schema(factory):return {**base,'status':'blocked','errorCode':'ISSUER_SAMPLE_SCHEMA_REQUIRED'}
            return {**base,'status':'preview','sampleEnabled':policy.sampleEnabled,
                'memberGraphPrerequisites':member_graph_prerequisites(factory,catalog,clock()),
                'groups':[{'fundCode':doc.fundCode,'state':_store(factory,catalog,recovery).inspect(_make_plan(factory,catalog,doc.fundCode,clock(),recovery),clock())}
                    for doc in _documents(catalog,recovery)]}
    engine,lock,acquired=None,None,False;result={}
    try:
        engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine);acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:result.update({**base,'status':'locked'});return result
        if execution_schema(factory):result.update({**base,'status':'blocked','errorCode':'ISSUER_SAMPLE_SCHEMA_REQUIRED'});return result
        result.update(execute_issuer_samples(factory,catalog,clock=clock,transport=transport,before_send=before_send,recovery=recovery,
            rate_limiter=RequestRateLimiter(catalog.official_policy.requestsPerMinute)));return result
    except Exception:result.update({**base,'status':'blocked','errorCode':'ISSUER_SAMPLE_RUNTIME_FAILED'});return result
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:result.setdefault('cleanupErrors',[]).append(stage)
