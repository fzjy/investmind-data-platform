"""官方地产/财政组的有界执行；只采集，不发布或启用调度器。"""
import asyncio
from datetime import datetime, timezone

from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError, load_extension_config
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.store import FAILURE_CODES
from app.macro.official_macro_store import OfficialMacroStore


def execute_official_macro_group(factory, config, group_id, *, transport=None, clock=None, rate_limiter=None, before_send=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    result = {'groupId':group_id,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,
              'insertedRows':0,'publicationRequested':False,'physicalBudgetEnforced':True,
              'supplierHttpCountBasis':'transport-attempt-upper-bound','sources':[]}
    try:
        plan = build_extension_plan(group_id,config,clock())
        store = OfficialMacroStore(factory)
        state = store.inspect(plan,clock())
        plan = state['plan']
        result.update(planFingerprint=plan['fingerprint'],planPersisted=state['runId'] is not None)
        if not config.groups[group_id].collectionEnabled:
            return {**result,'status':'disabled'}
        if state['status']=='blocked':
            entry=next(s for s in state['documents'] if s['phase'] in ('reserved','failed'))
            return {**result,'status':'blocked','errorCode':entry['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN'}
        for entry in state['documents']:
            doc,key=entry['document'],entry['runId']
            if entry['phase']=='completed':
                continue
            if entry['phase']=='new':
                if result['requestCount']>=plan['limits']['maxHttpPerRun']:
                    break
                request=store.request_for(plan,doc,clock())
                if request is None:
                    return {**result,'status':'source_pending','errorCode':'OFFICIAL_MACRO_NO_OBSERVED_REPORT'}
                if rate_limiter is not None:
                    rate_limiter.acquire()
                if before_send is not None:
                    before_send()
                current=load_extension_config()
                current_plan=build_extension_plan(group_id,current,datetime.fromisoformat(plan['scheduledAt']))
                if not current.groups[group_id].collectionEnabled or current_plan!=plan:
                    return {**result,'status':'blocked','errorCode':'EXTENSION_COLLECTION_CHANGED'}
                result['readOnly']=False
                try:
                    key=store.reserve(plan,doc,clock())
                except Exception:
                    return {**result,'status':'blocked','errorCode':'EXTENSION_RESERVATION_UNCERTAIN',
                            'reservationUncertain':True,'requestCount':result['requestCount']+1}
                result['requestCount']+=1
                result['supplierHttp']+=1
                result['planPersisted']=True
                try:
                    client=ExtensionClient('public-official-document',transport=transport)
                    body=bytes(asyncio.run(client._read(plan,'GET',request['url'])))
                    result['responseCount']+=1
                except Exception as error:
                    code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                    try:
                        store.fail(key,code,clock())
                    except Exception:
                        code='EXTENSION_ATTEMPT_UNCERTAIN'
                    return {**result,'status':'blocked','errorCode':code,'runId':key}
                try:
                    store.receive(key,body,clock())
                except Exception:
                    return {**result,'status':'blocked','errorCode':'EXTENSION_RAW_RECEIVE_PENDING','runId':key}
            result['readOnly']=False
            try:
                replay=store.replay(key,clock())
            except Exception:
                return {**result,'status':'replay_pending','errorCode':'OFFICIAL_MACRO_NORMALIZATION_PENDING','runId':key}
            result['insertedRows']+=replay.get('insertedRows',0)
            result['sources'].append({'documentId':doc['id'],**replay})
        final=store.inspect(plan,clock())
        return {**result,'status':final['status'],'reservedCount':final['reservedCount']}
    except Exception:
        return {**result,'status':'blocked','errorCode':'OFFICIAL_MACRO_EXECUTION_UNCERTAIN' if not result['readOnly'] else 'OFFICIAL_MACRO_EXECUTION_INVALID'}
