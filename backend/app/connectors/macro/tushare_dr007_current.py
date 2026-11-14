"""生产DR007长窗口精确分片；失败不返回部分观测。"""
from datetime import timedelta

from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderSchemaError
from app.connectors.macro.tushare_dr007 import CHINA_TIME, Dr007Client, fetch_dr007_history, validate_dr007_source
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


def fetch_dr007_current(client: Dr007Client, series: MacroSeriesDefinition,
                        source: MacroSourceDefinition, window: MacroFetchWindow) -> ProviderFetchResult:
    raw: dict[str, object] = {'sourceUrl': str(source.source_url), 'pages': []}
    try:
        budget = source.params.get('maxPages', '')
        if (set(source.params) != {'ts_code', 'maxPages'} or not budget.isascii()
                or not budget.isdecimal() or not 1 <= int(budget) <= 240):
            raise ProviderSchemaError('DR007_CONTRACT_INVALID')
        historical = source.model_copy(update={'params': {'ts_code': source.params['ts_code']}})
        validate_dr007_source(series, historical)
        if (window.as_of.utcoffset() is None or window.start > window.end
                or window.end > window.as_of.astimezone(CHINA_TIME).date()):
            raise ProviderSchemaError('DR007_WINDOW_INVALID')
        if ((window.end-window.start).days // 31 + 1) > int(budget):
            raise ProviderSchemaError('DR007_REQUEST_BUDGET_EXCEEDED')
    except ProviderSchemaError as error:
        return ProviderFetchResult('tushare', 'failed', (), raw, 0, error.sanitized_copy(request_count=0))
    start, count = window.start, 0
    pages: list[dict[str, object]] = []
    observations = []
    while start <= window.end:
        end = min(start+timedelta(days=30), window.end)
        result = fetch_dr007_history(client, series, historical, MacroFetchWindow(start,end,window.as_of))
        count += result.request_count
        pages.extend(result.raw_payload['pages'])
        raw['pages'] = pages
        if result.status not in {'success','no_change'}:
            return ProviderFetchResult('tushare','failed',(),raw,count,
                result.error.sanitized_copy(request_count=count) if result.error else None)
        observations.extend(result.observations)
        start = end+timedelta(days=1)
    return ProviderFetchResult('tushare','success' if observations else 'no_change',tuple(observations),raw,count)
