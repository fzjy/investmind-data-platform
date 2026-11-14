"""统一扩展入口中的两组交易所聚合；固定槽、零重试、只采集不发布。"""
import asyncio
from datetime import date,datetime,timezone

from sqlalchemy import select

from app.db.models.core import TradeCalendar
from app.macro.exchange_summary_client import ExchangeSummaryClient
from app.macro.exchange_summary_store import ExchangeSummaryStore,plan_from_database
from app.macro.extensions.config import ExtensionError,load_extension_config
from app.macro.extensions.store import FAILURE_CODES


def current_policy_matches(factory,config,plan):
    policy=config.groups[plan['groupId']]
    if not policy.collectionEnabled or policy.model_dump(exclude={'collectionEnabled'})!=plan['groupPolicy']:return False
    proof=plan['calendarEvidence']['rows'];low=min(r['date'] for r in proof);high=max(r['date'] for r in proof)
    with factory() as session:
        rows=session.scalars(select(TradeCalendar).where(TradeCalendar.exchange.in_(policy.calendarExchanges),
            TradeCalendar.cal_date>=date.fromisoformat(low),TradeCalendar.cal_date<=date.fromisoformat(high))
            .order_by(TradeCalendar.cal_date,TradeCalendar.exchange)).all()
        actual=[{'exchange':r.exchange,'date':r.cal_date.isoformat(),'open':r.is_open,
                 'previous':r.pretrade_date.isoformat() if r.pretrade_date else None} for r in rows]
    return actual==proof


def execute_exchange_group(factory,config,group_id,token,*,transport=None,clock=None,rate_limiter=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    result={'groupId':group_id,'readOnly':True,'supplierHttp':0,'requestCount':0,'responseCount':0,'insertedRows':0,
            'publicationRequested':False,'physicalBudgetEnforced':True,'supplierHttpCountBasis':'transport-attempt-upper-bound'}
    try:
        plan=plan_from_database(factory,config,group_id,clock());store=ExchangeSummaryStore(factory)
        state=store.inspect(plan,clock());plan=state['plan']
        result.update(planFingerprint=plan['fingerprint'],targetDate=plan['targetDate'],planPersisted=state['runId'] is not None)
        if not config.groups[group_id].collectionEnabled:return {**result,'status':'disabled'}
        if state['status']=='blocked':
            bad=next(e for e in state['documents'] if e['phase'] in ('reserved','failed'))
            return {**result,'status':'blocked','errorCode':bad['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN'}
        for entry in state['documents']:
            doc,key=entry['document'],entry['runId']
            if entry['phase']=='completed':continue
            if entry['phase']=='new':
                if result['requestCount']>=plan['limits']['maxHttpPerRun']:break
                if not token:return {**result,'status':'blocked','errorCode':'EXTENSION_AUTH_ERROR'}
                if rate_limiter is not None:rate_limiter.acquire()
                if before_send is not None:before_send()
                if not current_policy_matches(factory,load_extension_config(),plan):
                    return {**result,'status':'blocked','errorCode':'EXTENSION_COLLECTION_CHANGED'}
                result['readOnly']=False
                try:key=store.reserve(plan,doc,clock())
                except Exception:
                    return {**result,'status':'blocked','errorCode':'EXTENSION_RESERVATION_UNCERTAIN',
                            'reservationUncertain':True,'requestCount':result['requestCount']+1}
                result['requestCount']+=1;result['supplierHttp']+=1;result['planPersisted']=True
                try:
                    data=asyncio.run(ExchangeSummaryClient(token,transport=transport).fetch(plan,doc))
                    result['responseCount']+=1
                except Exception as error:
                    code=str(error) if isinstance(error,ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                    try:store.fail(key,code,clock())
                    except Exception:code='EXTENSION_ATTEMPT_UNCERTAIN'
                    return {**result,'status':'blocked','errorCode':code,'runId':key}
                try:store.receive(key,data,clock())
                except Exception:return {**result,'status':'blocked','errorCode':'EXTENSION_RAW_RECEIVE_PENDING','runId':key}
            try:
                replay=store.replay(key,clock())
                result['insertedRows']+=replay['insertedRows'];result['readOnly']=result['readOnly'] and replay['readOnly']
            except Exception:return {**result,'status':'replay_pending','errorCode':'EXTENSION_CORE_REPLAY_PENDING','runId':key}
        final=store.inspect(plan,clock())
        return {**result,'status':final['status']}
    except Exception:
        return {**result,'status':'blocked','errorCode':'EXTENSION_EXECUTION_INVALID'}
