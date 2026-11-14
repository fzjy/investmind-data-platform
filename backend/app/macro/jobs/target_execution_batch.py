"""单根手动批次；原子预留先于单次HTTP，Candidate缺口先重放。"""
from contextlib import ExitStack
from datetime import UTC,datetime
from uuid import uuid4

from app.connectors.tushare.target_execution_client import TargetExecutionClient,TargetExecutionError
from app.macro.target_execution_plan import step_request
from app.macro.target_execution_store import TargetExecutionStore


def execute_target_batch(factory,catalog,selection,as_of,timezone,token,*,transport=None,now=None):
    policy=catalog.evidence_policy
    result={'status':'partial','readOnly':False,'executorImplemented':True,'physicalBudgetImplemented':True,
        'requestCount':0,'responseCount':0,'membershipReady':False,'publicationRequested':False,
        'httpLimit':policy.max_http_per_run,'rootHttpLimit':policy.max_http_per_plan,
        'requestCountBasis':'committed-reservation-attempt-upper-bound'}
    if not policy.collection_enabled:
        return {**result,'status':'disabled','readOnly':True,'errorCode':'TARGET_COLLECTION_DISABLED'}
    clock=lambda:datetime.now(UTC) if now is None else now
    store=TargetExecutionStore(factory)
    root=store.prepare(catalog,selection,as_of,timezone)
    progress=store.progress(root)
    result.update(planFingerprint=root['fingerprint'],frozenAsOfDate=root['snapshot']['frozenAsOfDate'])
    if progress['blockedReasons']:
        return {**result,**progress,'status':'blocked','readOnly':True,'errorCode':progress['blockedReasons'][0]}
    if not token and progress['rootReservedCount']==0 and not progress['processingCompleted']:
        return {**result,**progress,'status':'blocked','readOnly':True,'errorCode':'TARGET_TUSHARE_TOKEN_REQUIRED'}
    root=store.prepare(catalog,selection,as_of,timezone,persist=True)
    progress=store.progress(root,persist=True)
    run_id=str(uuid4());client=None;error=None
    with ExitStack() as stack:
        while not progress['processingCompleted']:
            if progress['blockedReasons']:error=progress['blockedReasons'][0];break
            if result['requestCount']>=policy.max_http_per_run:break
            if progress['rootReservedCount']>=policy.max_http_per_plan:error='TARGET_ROOT_BUDGET_EXHAUSTED';break
            if not token:error='TARGET_TUSHARE_TOKEN_REQUIRED';break
            if client is None:client=stack.enter_context(TargetExecutionClient(token,policy,transport=transport))
            step=progress['next'];reserved=None
            def reserve():
                nonlocal reserved
                reserved=store.reserve(root,step,run_id,clock());result['requestCount']+=1
            try:
                response=client.call(step_request(step),reserve)
            except TargetExecutionError as exc:
                error=exc.code
                if reserved is not None:store.fail(reserved,error,clock())
                progress=store.progress(root);break
            except ValueError as exc:
                # 仅预留门有权返回这些安全码；未知错误交给外层脱敏。
                code=str(exc)
                error=code if code in {'TARGET_ROOT_BUDGET_EXHAUSTED','TARGET_RUN_BUDGET_EXHAUSTED',
                    'TARGET_RATE_LIMITED','TARGET_CLOCK_REGRESSION','TARGET_RESERVATION_CONFLICT',
                    'TARGET_ATTEMPT_FAILED','TARGET_ATTEMPT_UNCERTAIN','TARGET_EXECUTION_INVALID'} else 'TARGET_EXECUTION_INVALID'
                progress=store.progress(root);break
            store.receive(reserved,response,clock(),policy);result['responseCount']+=1
            progress=store.progress(root,persist=True)
    status=('completed-with-gaps' if progress['terminalGaps'] else 'completed') if progress['processingCompleted'] else 'partial'
    if error:status='rate-limited' if error=='TARGET_RATE_LIMITED' else 'blocked'
    return {**result,**progress,'status':status,'supplierHttp':result['requestCount'],
        **({'errorCode':error} if error else {})}
