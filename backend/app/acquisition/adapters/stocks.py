from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.acquisition.contracts import Attempt, Evidence, StateKey, utc
from app.acquisition.repository import StateRepository

API_ENTRIES = {
    'daily':'stock-daily', 'daily_basic':'stock-daily-basic', 'adj_factor':'stock-adjustment',
    'balancesheet':'stock-balance', 'income':'stock-income', 'cashflow':'stock-cashflow',
    'fina_indicator':'stock-financial-indicator', 'disclosure_date':'stock-disclosure',
    'dividend':'stock-dividend', 'trade_cal':'trade-calendar', 'stock_basic':'stock-basic',
    'repurchase':'stock-repurchase', 'share_float':'stock-unlock', 'forecast':'stock-forecast',
    'express':'stock-express','stk_holdernumber':'stock-holder-count','top10_holders':'stock-top-holders',
    'fina_mainbz':'stock-business',
}


def bind_stock_attempt(plan, *, identity, attempt_id, started_at, scope_kind):
    entry = API_ENTRIES.get(plan.api_name)
    kind, param = ('exchange', 'exchange') if entry == 'trade-calendar' else ('security', 'ts_code')
    subject = plan.params.get(param)
    if not entry or not subject or ',' in subject:
        return ()
    scope = {'source':'tushare','api':plan.api_name,'plan':identity,
             'params':{k:v for k,v in plan.params.items() if k not in ('offset','limit')}}
    return (Attempt(StateKey(entry,'',kind,subject,scope_kind), attempt_id,started_at,scope,'collector'),)


def stock_attempts(plan, identity, *, scope_kind='routine', run_id=None):
    attempts = bind_stock_attempt(plan, identity=identity, attempt_id=str(uuid4()),
                                  started_at=datetime.now(UTC), scope_kind=scope_kind)
    if run_id:
        return tuple(replace(a,scope={**a.scope,'runId':run_id}) for a in attempts)
    return attempts


def stock_result(session, attempts, receipt, checked, *, cached=None):
    repo = StateRepository(session)
    for attempt in attempts:
        if not repo.begin(attempt):
            continue
        status = {'completed':'success','pending':'unrecorded'}.get(receipt['status'],'failed')
        scope = {**attempt.scope, 'checkKind':'cached' if cached else 'supplier'}
        if cached:
            scope['rawAt'] = utc(cached.captured_at).isoformat()
        if status == 'success' and receipt['received'] == 0 and attempt.key.entry_id == 'stock-dividend':
            scope['confirmedEmpty'] = True
        evidence = Evidence(status,checked,checked if status == 'success' else None,
                            'STOCK_RESPONSE_REJECTED' if status == 'failed' else None,
                            {'success':'请求范围已校验并提交','unrecorded':'目标数据尚未核全',
                             'failed':'响应校验或入库失败'}[status], scope, ({'batchId':receipt['batchId']},))
        repo.finish(attempt,evidence)


def read_stock_evidence(session, keys, *, now):
    return StateRepository(session).read(keys)
