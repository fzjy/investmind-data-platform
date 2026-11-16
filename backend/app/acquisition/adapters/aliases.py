"""供应商原始接口与业务序列共用一份回执。"""
from app.acquisition.contracts import StateKey, utc
from app.acquisition.tracking import record_terminal

SERIES = {
    ('market-margin','SSE'): 'cn.market.margin_balance.sse',
    ('market-margin','SZSE'): 'cn.market.margin_balance.szse',
    ('market-margin','BSE'): 'cn.market.margin_balance.bse',
    ('market-daily-summary','SSE'): 'cn.market.a_share_float_mv.sh',
    ('market-daily-summary','SZSE'): 'cn.market.a_share_float_mv.sz',
    ('fx-daily','USDCNH.FXCM'): 'fx.usdcnh',
    ('source-cn_gdp','source-cn_gdp'): 'cn.gdp.nominal_cumulative',
}


def record_source_document(session,row,checked):
    plan = row.details['plan']
    document = row.details.get('document',{})
    query = document.get('query') or plan.get('query',{})
    api = query.get('api')
    sid = document.get('seriesId') or plan.get('seriesId')
    if api not in ('margin','daily_info','fx_daily','cn_gdp'):
        return
    for (entry,subject), series in SERIES.items():
        if series != sid:
            continue
        kind = 'series' if api == 'cn_gdp' else 'instrument' if api == 'fx_daily' else 'exchange'
        if row.status == 'completed' and not any(r.get('seriesId',plan.get('seriesId')) == sid for r in row.details.get('coreBindings',[])):
            return
        if row.status not in ('completed','failed'):
            return
        record_terminal(session,StateKey(entry,'',kind,subject),started_at=utc(row.started_at),checked_at=checked,
            status='success' if row.status=='completed' else 'failed', code=row.error_message,
            scope={'source':'tushare','api':api,'params':query['params']},references=({'runId':row.id},))
