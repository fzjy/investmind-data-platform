"""来源先行的正常有界入口；复用声明，只为CSI/同日PCF预留实际请求。"""
import asyncio
from datetime import datetime,timezone

from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.macro.fund_member_graph import GRAPH_BOUNDS,build_fund_graph_intent
from app.macro.fund_member_graph_store import FundMemberGraphStore,load_source_contract_state
from app.macro.official_member_budget import daily_budget
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import FAILURE_CODES


def current_policy_matches(catalog,intent,daily_budget_enabled=None):
    try:
        if daily_budget_enabled is not None and (catalog.official_policy is None
            or catalog.official_policy.dailyBudgetEnabled!=daily_budget_enabled):return False
        p=catalog.catalog_member_policy
        if p is None or not p.collectionEnabled or not p.memberGraphEnabled or catalog.fingerprint!=intent['catalogFingerprint']:return False
        scope=next(g for g in p.memberGraphs if g.code==intent['target']['code'])
        doc=next(d for d in p.discoveryDocuments if d.id==scope.discoveryDocumentId)
        target=next(t for t in catalog.targets if t.code==scope.code)
        return (doc.model_dump()==intent['sourceContract']['plan']['document']
            and {'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint}==intent['target']
            and {k:getattr(catalog.official_policy,k) for k in GRAPH_BOUNDS}==intent['limits'])
    except Exception:return False


def execute_fund_graphs(factory,catalog,*,clock=None,transport=None,rate_limiter=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'publicationRequested':False,
        'purpose':'member-proof-for-registered-graphs-only','wholeCatalogCompletionClaimed':False,'groups':[],
        'physicalBudgetEnforced':True,'supplierHttpCountBasis':'transport-attempt-upper-bound'}
    policy=catalog.catalog_member_policy
    if policy is None or not policy.collectionEnabled or not policy.memberGraphEnabled:return {**result,'status':'disabled','configuredGraphsCompleted':False}
    store=FundMemberGraphStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled)
    for scope in policy.memberGraphs:
        group={'targetCode':scope.code,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'witnessReady':False}
        try:
            parent=load_source_contract_state(factory,catalog,scope.code,clock());proof=parent['proof'];group['sourceContractPhase']=parent['phase']
            if proof is None:
                if parent['phase']=='failed':group.update(status='blocked',errorCode=parent['errorCode'])
                elif parent['phase']=='reserved':group.update(status='blocked',errorCode='EXTENSION_ATTEMPT_UNCERTAIN')
                elif parent['phase']=='received':group.update(status='replay_pending',errorCode='FUND_MEMBER_GRAPH_SOURCE_CONTRACT_REPLAY_PENDING')
                else:group.update(status='source_pending',errorCode='FUND_MEMBER_GRAPH_SOURCE_CONTRACT_PENDING')
            else:
                intent=build_fund_graph_intent(catalog,scope.code,proof,clock());state=store.inspect(intent,clock());intent=state['intent']
                group.update(intentFingerprint=intent['fingerprint'],firstBusinessDate=intent['firstBusinessDate'],dailyBudget=state['dailyBudget'])
                for kind in ('csi-members','fund-pcf'):
                    state=store.inspect(intent,clock())
                    if state['status']=='blocked':
                        bad=next(s for s in state['sources'] if s['phase'] in ('reserved','failed'))
                        group.update(status='blocked',errorCode=bad['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN');break
                    source=next(s for s in state['sources'] if s['kind']==kind);key=source['runId']
                    if source['phase']=='completed':continue
                    if source['phase']=='new':
                        if result['requestCount']+group['requestCount']>=catalog.official_policy.maxHttpPerRun or state['dailyBudget']['remaining']==0:
                            group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING');break
                        if rate_limiter is not None:rate_limiter.acquire()
                        if before_send is not None:before_send()
                        if not current_policy_matches(load_enterprise_target_catalog(),intent,catalog.official_policy.dailyBudgetEnabled):
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED');break
                        group['readOnly']=False
                        try:key=store.reserve(intent,kind,clock())
                        except Exception as error:
                            if isinstance(error,ValueError) and str(error)=='OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED':
                                group.update(status='budget_pending',errorCode='OFFICIAL_MEMBER_DAILY_OR_RUN_BUDGET_PENDING')
                            else:group.update(status='blocked',errorCode='FUND_MEMBER_GRAPH_RESERVATION_UNCERTAIN',requestCount=group['requestCount']+1)
                            break
                        group['requestCount']+=1
                        if not current_policy_matches(load_enterprise_target_catalog(),intent,catalog.official_policy.dailyBudgetEnabled):
                            group.update(status='blocked',errorCode='OFFICIAL_MEMBER_COLLECTION_CHANGED');break
                        group['supplierHttp']+=1
                        try:
                            from app.db.models.ops import JobRun
                            with factory() as session:leaf=store._run(session,session.get(JobRun,key))['plan']
                            from app.macro.fsfund_contracts import official_transport
                            reader=ExtensionClient('public-official-document',transport=official_transport(leaf['document'],transport))
                            bounds={k:intent['limits'][k] for k in ('requestTimeoutSeconds','maxResponseBytes')}
                            from app.macro.nffund_contracts import request_options
                            method,options=request_options(leaf['document'])
                            body=bytes(asyncio.run(reader._read({'limits':bounds},method,leaf['document']['url'],**options)));group['responseCount']+=1
                        except Exception as error:
                            code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                            try:store.fail(key,code,clock())
                            except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                            group.update(status='blocked',errorCode=code);break
                        try:store.receive(key,body,clock())
                        except Exception:group.update(status='blocked',errorCode='FUND_MEMBER_GRAPH_RAW_RECEIVE_PENDING');break
                    try:
                        replay=store.replay(key,clock());group['readOnly']=group['readOnly'] and replay['readOnly']
                    except Exception:group.update(status='replay_pending',errorCode='FUND_MEMBER_GRAPH_NORMALIZE_PENDING');break
                if 'status' not in group:
                    state=store.inspect(intent,clock());group.update(status='completed' if state['witnessReady'] else state['status'],witnessReady=state['witnessReady'])
        except Exception:group.update(status='blocked',errorCode='FUND_MEMBER_GRAPH_EXECUTION_INVALID')
        try:
            checked=clock()
            with factory() as session:group['dailyBudget']=daily_budget(session,checked,catalog.official_policy.maxHttpPerDay if catalog.official_policy.dailyBudgetEnabled else None)
            group['dailyBudgetCheckedAt']=checked.isoformat()
        except Exception:
            group['status']='blocked';group.setdefault('errorCode','FUND_MEMBER_GRAPH_BUDGET_INVALID')
        result['groups'].append(group)
        for k in ('supplierHttp','requestCount','responseCount'):result[k]+=group[k]
        result['readOnly']=result['readOnly'] and group['readOnly']
    result['configuredGraphsCompleted']=bool(result['groups']) and all(g['witnessReady'] for g in result['groups'])
    result['status']=('blocked' if any(g['status'] in ('blocked','replay_pending') for g in result['groups']) else
        'completed' if result['configuredGraphsCompleted'] else 'partial')
    return result


def run_fund_graphs(settings,*,dry_run=False,transport=None,clock=None,before_send=None):
    from sqlalchemy.engine import make_url
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.macro.jobs.official_member_runtime import LOCK_KEY
    from app.macro.extensions.store import execution_schema
    from app.shared.readonly_database import open_readonly_factory
    base={'readOnly':True,'supplierHttp':0,'publicationRequested':False}
    catalog=load_enterprise_target_catalog();policy=catalog.catalog_member_policy
    if policy is None or not dry_run and (not policy.collectionEnabled or not policy.memberGraphEnabled):return {**base,'status':'disabled'}
    if settings.data_backend!='database' or not settings.database_url:return {**base,'status':'blocked','errorCode':'FUND_MEMBER_GRAPH_DATABASE_REQUIRED'}
    url=make_url(settings.database_url)
    if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':return {**base,'status':'blocked','errorCode':'FUND_MEMBER_GRAPH_POSTGRES_REQUIRED'}
    clock=clock or (lambda:datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            groups=[]
            for scope in policy.memberGraphs:
                parent=load_source_contract_state(factory,catalog,scope.code,clock());proof=parent['proof']
                state=FundMemberGraphStore(factory,daily_budget_enabled=catalog.official_policy.dailyBudgetEnabled).inspect(build_fund_graph_intent(catalog,scope.code,proof,clock()),clock()) if proof else None
                groups.append({'targetCode':scope.code,'state':state,'sourceContractReady':proof is not None,
                    'sourceContractPhase':parent['phase'],'sourceContractErrorCode':parent['errorCode']})
            return {**base,'status':'preview','memberGraphEnabled':policy.memberGraphEnabled,'groups':groups}
    engine,lock,acquired=None,None,False;result={}
    try:
        engine,factory=create_engine_and_session(settings.database_url);lock=job_lock_for_engine(engine);acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:result.update({**base,'status':'locked'});return result
        if execution_schema(factory):result.update({**base,'status':'blocked','errorCode':'FUND_MEMBER_GRAPH_SCHEMA_REQUIRED'});return result
        result.update(execute_fund_graphs(factory,catalog,clock=clock,transport=transport,before_send=before_send,
            rate_limiter=RequestRateLimiter(catalog.official_policy.requestsPerMinute)));return result
    except Exception:result.update({**base,'status':'blocked','errorCode':'FUND_MEMBER_GRAPH_RUNTIME_FAILED'});return result
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:result.setdefault('cleanupErrors',[]).append(stage)
