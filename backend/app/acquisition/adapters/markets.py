from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal

MARKET_APIS = {
    'fund_daily': ('etf-daily','instrument',''), 'fund_share': ('etf-shares','instrument',''),
    'index_daily': ('index-daily','instrument',''), 'index_global': ('index-global','instrument',''),
    'fut_daily': ('commodity-daily','instrument',''), 'fut_mapping': ('commodity-mapping','instrument',''),
    'fut_basic': ('commodity-contracts','instrument',''), 'fut_trade_cal': ('commodity-calendar','exchange',''),
    'region': ('security-region','security',''), 'limit_status': ('stock-daily-basic','security','limit_status'),
}


def commodity_subject(params):
    if params.get('fut_code'):
        exchange = {'SHFE':'SHF','DCE':'DCE','CZCE':'ZCE','INE':'INE','CFFEX':'CFX','GFEX':'GFE'}.get(params.get('exchange'),params.get('exchange'))
        return params['fut_code']+'.'+exchange
    return params.get('ts_code') or params.get('exchange')


def record_market(session, api, subject, params, captured, *, batch_id=None, error=None, run_id=None):
    if api.startswith('fut_'):
        subject = commodity_subject(params)
    if not subject or api not in MARKET_APIS:
        return
    entry, kind, item = MARKET_APIS[api]
    refs = tuple({name:value} for name,value in (('batchId',batch_id),('runId',run_id)) if value)
    record_terminal(session, StateKey(entry,item,kind,subject), started_at=captured, checked_at=captured,
                    status='failed' if error else 'success', code=error,
                    scope={'source':'tushare','api':api,'params':params}, references=refs,
                    reason='采集失败' if error else '请求和数据校验通过')
