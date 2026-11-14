"""DR007 实际供应商边界；仅 HTTP 响应受控。"""
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.tushare import TushareMacroProvider
from app.connectors.tushare.client import TushareClient
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition

SOURCE = {
    'provider': 'tushare', 'priority': 1, 'series': 'DR007.IB', 'api': 'repo_daily',
    'field': 'weight', 'transform': 'identity', 'sourceUrl': 'https://tushare.pro/document/2?doc_id=256',
    'termsUrl': 'https://tushare.pro/document/1?doc_id=20', 'accessMode': 'api',
    'displayAllowed': True, 'params': {'ts_code': 'DR007.IB'},
    'responseFields': ['trade_date', 'ts_code', 'repo_maturity', 'weight'],
}
WINDOW = MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 10, 8, tzinfo=UTC))


def response():
    return {'code': 0, 'msg': '', 'data': {'fields': ['trade_date', 'ts_code', 'repo_maturity', 'weight'],
        'items': [['20260930', 'DR007.IB', 'DR007', 1.3665], ['20260929', 'DR007.IB', 'DR007', 1.3862]]}}


def fetch(payload=None, *, source=None, window=WINDOW, metadata=None, status=200):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=deepcopy(payload if payload is not None else response()))
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.rate.dr007')
    if metadata:
        series = series.model_copy(update=metadata)
    with TushareClient('controlled-token', transport=httpx.MockTransport(handler), max_retries=0) as client:
        result = TushareMacroProvider(client).fetch(series,
            MacroSourceDefinition.model_validate(source or SOURCE), window)
    return result, requests


def test_dr007_uses_exact_weight_and_preserves_response_without_fake_release():
    result, requests = fetch()
    assert result.status == 'success' and result.request_count == 1
    assert [o.value for o in result.observations] == [Decimal('1.3665'), Decimal('1.3862')]
    assert all(o.provider_series_id == 'DR007.IB' and o.released_at is None and o.revision is None
        for o in result.observations)
    assert len(requests) == 1
    assert b'"ts_code":"DR007.IB"' in requests[0].content
    assert result.raw_payload['pages'][0]['response']['items'][0] == ['20260930', 'DR007.IB', 'DR007', 1.3665]
    assert 'controlled-token' not in str(result.raw_payload)


@pytest.mark.parametrize('index,value', [(1,'R007.IB'),(1,'FDR007.IB'),(2,'FDR007'),(2,'R007'),
    (3,None),(3,''),(3,True),(3,'NaN'),(3,'Infinity'),(3,'not-a-number'),
    (0,'20260831'),(0,'20261001'),(0,'20260931'),(0,'2026-09-30')])
def test_dr007_invalid_row_discards_all_observations_but_keeps_raw(index, value):
    payload=response()
    payload['data']['items'][1][index]=value
    result, requests=fetch(payload)
    assert result.status == 'failed' and result.observations == () and result.request_count == 1
    assert len(result.raw_payload['pages']) == 1 and len(requests) == 1


@pytest.mark.parametrize('update', [{'field':'close'},{'field':'weight_r'},{'series':'FDR007.IB'},
    {'api':'shibor'}, {'params':{'ts_code':'DR007.IB','other':'x'}},
    {'responseFields':['trade_date','ts_code','weight']}, {'sourceUrl':'https://example.test/api'},
    {'transform':'pc1'}, {'accessMode':'public_page'}])
def test_dr007_unverified_source_does_not_call_http(update):
    result, requests=fetch(source={**SOURCE, **update})
    assert result.status == 'failed' and result.request_count == 0 and requests == []


@pytest.mark.parametrize('metadata', [{'region':'US'},{'unit':'CNY'},{'frequency':'monthly'}])
def test_dr007_bad_canonical_metadata_is_refused(metadata):
    result, requests=fetch(metadata=metadata)
    assert result.status == 'failed' and requests == []


@pytest.mark.parametrize('window', [
    MacroFetchWindow(date(2026,9,30),date(2026,9,1),WINDOW.as_of),
    MacroFetchWindow(date(2026,8,1),date(2026,9,30),WINDOW.as_of),
    MacroFetchWindow(date(2026,9,1),date(2026,9,30),datetime(2026,9,20,tzinfo=UTC)),
    MacroFetchWindow(date(2026,9,1),date(2026,9,30),datetime(2026,10,8)),
])
def test_dr007_invalid_window_is_refused_before_http(window):
    result, requests=fetch(window=window)
    assert result.status == 'failed' and requests == []


@pytest.mark.parametrize('change', ['duplicate','conflict','fields','limit','too-many-days'])
def test_dr007_rejects_duplicates_missing_fields_and_truncation(change):
    payload=response()
    if change in {'duplicate','conflict'}:
        payload['data']['items'][1][0]='20260930'
    elif change == 'fields':
        payload['data']['fields'][2]='close'
    else:
        payload['data']['items'] *= (1000 if change == 'limit' else 16)
    result, _=fetch(payload)
    assert result.status == 'failed' and result.observations == ()


def test_dr007_proven_empty_response_is_no_change():
    payload=response()
    payload['data']['items']=[]
    result, _=fetch(payload)
    assert result.status == 'no_change' and result.request_count == 1 and result.error is None


@pytest.mark.parametrize('code,message,status', [(402,'权限不足',200),(401,'token invalid',200),
    (429,'每分钟限流',200),(0,'',403),(0,'',500)])
def test_dr007_failed_http_or_permission_has_no_retry_or_secret(code,message,status):
    result, requests=fetch({'code':code,'msg':message},status=status)
    assert result.status == 'failed' and result.observations == () and result.request_count == 1
    assert len(requests) == 1 and 'controlled-token' not in str(result)


def test_dr007_china_date_boundary_is_accepted():
    result,_=fetch(window=MacroFetchWindow(WINDOW.start,WINDOW.end,datetime(2026,9,29,16,tzinfo=UTC)))
    assert result.status == 'success'
