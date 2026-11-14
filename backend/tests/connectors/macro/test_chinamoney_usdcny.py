from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json

import httpx
import pytest

from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider
from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.transport import MacroHttpTransport
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


URL = 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew'
STATIC_URL = 'https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json'
CURRENCIES = [
    'USD/CNY', 'EUR/CNY', '100JPY/CNY', 'HKD/CNY', 'GBP/CNY', 'AUD/CNY',
    'NZD/CNY', 'SGD/CNY', 'CHF/CNY', 'CAD/CNY', 'CNY/MOP', 'CNY/MYR',
    'CNY/RUB', 'CNY/ZAR', 'CNY/KRW', 'CNY/AED', 'CNY/SAR', 'CNY/HUF',
    'CNY/PLN', 'CNY/DKK', 'CNY/SEK', 'CNY/NOK', 'CNY/TRY', 'CNY/MXN', 'CNY/THB',
]


def _source() -> MacroSourceDefinition:
    return MacroSourceDefinition.model_validate({
        'provider': 'chinamoney', 'priority': 1, 'series': 'USD/CNY', 'api': 'CcprHisNew',
        'field': 'values', 'transform': 'identity', 'sourceUrl': URL,
        'termsUrl': 'https://www.chinamoney.com.cn/chinese/mtsjfu/',
        'accessMode': 'public_page', 'displayAllowed': True,
        'params': {'maxPages': '4'}, 'responseFields': ['date', 'values'],
    })


def _window() -> MacroFetchWindow:
    return MacroFetchWindow(date(2016, 10, 1), date(2016, 10, 31), datetime(2026, 10, 9, tzinfo=UTC))


def _pages() -> list[dict]:
    # 完整回显结构来自官方接口；日期和值为受控数据，不声称交易日完整。
    records = [
        {'date': (date(2016, 10, 31) - timedelta(days=i)).isoformat(), 'values': ['6.7008']}
        for i in range(11)
    ]
    return [{
        'head': {'version': '2.0', 'provider': 'CWAP', 'req_code': '', 'rep_code': '200',
                 'rep_message': '', 'ts': 1791544829822, 'producer': '',
                 'tstext': '2026-10-09 19:20:29'},
        'data': {'head': CURRENCIES.copy(), 'total': 11, 'pageTotal': 2,
                 'searchlist': ['USD/CNY'], 'startDate': '2016-10-01', 'endDate': '2016-10-31',
                 'currency': 'USD/CNY', 'pageNum': i + 1, 'pageSize': 10, 'flagMessage': ''},
        'records': deepcopy(records[:10] if i == 0 else records[10:]),
    } for i in range(2)]


def _fetch(*, pages=None, source=None, series=None, window=None, fail_page=None):
    pages = _pages() if pages is None else pages
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == 'POST'
        assert str(request.url).split('?', 1)[0] == URL
        assert json.loads(request.content) == {}
        page = int(request.url.params['pageNum'])
        if page == fail_page:
            return httpx.Response(403)
        return httpx.Response(200, json=pages[page - 1])

    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handler)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        result = ChinaMoneyMacroProvider(transport).fetch(
            series or MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.fx.usdcny'),
            source or _source(), window or _window(),
        )
    return result, requests


def test_history_provider_parses_selected_usdcny_two_pages_without_release_invention() -> None:
    result, requests = _fetch()
    assert result.status == 'success'
    assert len(result.observations) == 11
    assert result.observations[0].observed_at == date(2016, 10, 31)
    assert result.observations[-1].observed_at == date(2016, 10, 21)
    assert all(p.value == Decimal('6.7008') and p.provider_series_id == 'USD/CNY'
               and p.released_at is None and p.revision is None for p in result.observations)
    assert result.request_count == 2 and len(result.raw_payload['pages']) == 2
    assert [dict(r.url.params) for r in requests] == [
        {'startDate': '2016-10-01', 'endDate': '2016-10-31', 'currency': 'USD/CNY',
         'pageNum': '1', 'pageSize': '10'},
        {'startDate': '2016-10-01', 'endDate': '2016-10-31', 'currency': 'USD/CNY',
         'pageNum': '2', 'pageSize': '10'},
    ]


@pytest.mark.parametrize('changes,code', [
    ({'source_url': 'https://example.com/CcprHisNew'}, 'CHINAMONEY_HOST_NOT_ALLOWED'),
    ({'source_url': URL + '?token=source-secret-canary'}, 'CHINAMONEY_HOST_NOT_ALLOWED'),
    ({'source_url': URL + '#section'}, 'CHINAMONEY_HOST_NOT_ALLOWED'),
    ({'source_url': URL.replace('https://', 'https://user:password@')}, 'CHINAMONEY_HOST_NOT_ALLOWED'),
    ({'source_url': URL.replace('.cn/', '.cn:443/')}, 'CHINAMONEY_HOST_NOT_ALLOWED'),
    ({'api': 'CcprHis', 'source_url': STATIC_URL}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'series': 'USD/CNH'}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'provider': 'tushare'}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'field': 'close'}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'transform': 'pc1'}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'access_mode': 'api'}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'response_fields': ('values', 'date')}, 'CHINAMONEY_IDENTIFIER_INVALID'),
    ({'params': {}}, 'CHINAMONEY_PARAMS_INVALID'),
    ({'params': {'maxPages': '0'}}, 'CHINAMONEY_PARAMS_INVALID'),
    ({'params': {'maxPages': '5'}}, 'CHINAMONEY_PARAMS_INVALID'),
    ({'params': {'maxPages': '04'}}, 'CHINAMONEY_PARAMS_INVALID'),
    ({'params': {'maxPages': 4}}, 'CHINAMONEY_PARAMS_INVALID'),
    ({'params': {'maxPages': '4', 'currency': 'EUR/CNY'}}, 'CHINAMONEY_PARAMS_INVALID'),
])
def test_history_rejects_wrong_source_before_http(changes, code) -> None:
    result, requests = _fetch(source=_source().model_copy(update=changes))
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == code and result.request_count == 0 and requests == []
    assert 'source-secret-canary' not in json.dumps(result.raw_payload)


@pytest.mark.parametrize('changes', [
    {'id': 'cn.rate.dr007'}, {'unit': '%'}, {'region': 'US'},
    {'frequency': 'monthly'}, {'category': 'money_credit'},
])
def test_history_rejects_wrong_canonical_contract_before_http(changes) -> None:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.fx.usdcny')
    result, requests = _fetch(series=series.model_copy(update=changes))
    assert result.error.code == 'CHINAMONEY_IDENTIFIER_INVALID'
    assert result.request_count == 0 and requests == []


@pytest.mark.parametrize('window', [
    MacroFetchWindow(date(2006, 1, 3), date(2006, 1, 4), datetime(2026, 10, 9, tzinfo=UTC)),
    MacroFetchWindow(date(2016, 10, 1), date(2016, 11, 1), datetime(2026, 10, 9, tzinfo=UTC)),
    MacroFetchWindow(date(2016, 10, 31), date(2016, 10, 1), datetime(2026, 10, 9, tzinfo=UTC)),
    MacroFetchWindow(date(2016, 10, 1), date(2016, 10, 31), datetime(2026, 10, 9)),
    MacroFetchWindow(date(2026, 10, 9), date(2026, 10, 10), datetime(2026, 10, 9, tzinfo=UTC)),
])
def test_history_rejects_invalid_window_before_http(window) -> None:
    result, requests = _fetch(window=window)
    assert result.error.code == 'CHINAMONEY_WINDOW_INVALID'
    assert result.request_count == 0 and requests == []


def test_history_returns_explicit_empty_window_without_fabricating_observations() -> None:
    page = _pages()[0]
    page['data'].update(total=0, pageTotal=0)
    page['records'] = []
    result, requests = _fetch(pages=[page])
    assert result.status == 'no_change' and result.observations == ()
    assert result.request_count == 1 and len(requests) == 1
    assert result.raw_payload['pages'][0]['response']['data']['total'] == 0


def test_history_stops_after_first_page_when_server_exceeds_configured_budget() -> None:
    result, requests = _fetch(source=_source().model_copy(update={'params': {'maxPages': '1'}}))
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == 'CHINAMONEY_USDCNY_PAGE_BUDGET_EXCEEDED'
    assert result.request_count == 1 and len(requests) == 1
    assert len(result.raw_payload['pages']) == 1


@pytest.mark.parametrize('mutation,code', [
    ('header', 'CHINAMONEY_HEADER_INVALID'),
    ('currency', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('searchlist', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('head_missing', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('head_duplicate', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('bool_total', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('page_number', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('page_size', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('page_total', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('window_echo', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('message', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('truncated', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('extra_record', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('extra_column', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('wrong_date', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('duplicate', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('ascending', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('second_head', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('second_total', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('cross_duplicate', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
])
def test_history_rejects_drifted_pages_without_partial_observations(mutation, code) -> None:
    pages = _pages()
    page, data, records = pages[0], pages[0]['data'], pages[0]['records']
    if mutation == 'header':
        page['head']['provider'] = 'UNKNOWN'
    elif mutation == 'currency':
        data['currency'] = 'EUR/CNY'
    elif mutation == 'searchlist':
        data['searchlist'] = ['USD/CNY', 'EUR/CNY']
    elif mutation == 'head_missing':
        data['head'].remove('USD/CNY')
    elif mutation == 'head_duplicate':
        data['head'].append('USD/CNY')
    elif mutation == 'bool_total':
        data['total'] = True
    elif mutation == 'page_number':
        data['pageNum'] = 0  # 汇率不是国债的0-based回显。
    elif mutation == 'page_size':
        data['pageSize'] = 15
    elif mutation == 'page_total':
        data['pageTotal'] = 3
    elif mutation == 'window_echo':
        data['startDate'] = '2016-09-01'
    elif mutation == 'message':
        data['flagMessage'] = 'denied'
    elif mutation == 'truncated':
        records.pop()
    elif mutation == 'extra_record':
        records[0]['close'] = '7.0000'
    elif mutation == 'extra_column':
        records[0]['values'].append('7.0000')
    elif mutation == 'wrong_date':
        records[0]['date'] = '2016-11-01'
    elif mutation == 'duplicate':
        records[1]['date'] = records[0]['date']
    elif mutation == 'ascending':
        records[0], records[1] = records[1], records[0]
    elif mutation == 'second_head':
        pages[1]['data']['head'].reverse()
    elif mutation == 'second_total':
        pages[1]['data']['total'] = 12
        pages[1]['records'].append({'date': '2016-10-20', 'values': ['6.7008']})
    elif mutation == 'cross_duplicate':
        pages[1]['records'][0]['date'] = '2016-10-22'
    result, requests = _fetch(pages=pages)
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == code
    assert result.request_count == len(requests) == (2 if mutation.startswith('second_') or mutation == 'cross_duplicate' else 1)
    assert len(result.raw_payload['pages']) == len(requests)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '0', '-6.7', True, 6.7, None, '', ' 6.7 ', '1E1'])
def test_history_rejects_invalid_or_nonpositive_decimal(value) -> None:
    pages = _pages()
    pages[0]['records'][0]['values'] = [value]
    result, _ = _fetch(pages=pages)
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == 'CHINAMONEY_USDCNY_SCHEMA_INVALID'


def test_history_http_failure_keeps_received_raw_and_actual_request_count() -> None:
    result, requests = _fetch(fail_page=2)
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == 'HTTP_PERMISSION_DENIED' and result.error.retryable is False
    assert result.request_count == 2 and len(requests) == 2
    assert len(result.raw_payload['pages']) == 1


def test_history_raw_masks_unrequested_positional_secret_columns() -> None:
    pages = _pages()
    pages[0]['data']['head'].append('token')
    pages[0]['records'][0]['values'].append('positional-secret-canary')
    pages[0]['records'][0]['token'] = 'key-secret-canary'
    result, _ = _fetch(pages=pages)
    assert result.status == 'failed' and result.observations == ()
    evidence = json.dumps(result.raw_payload)
    assert 'positional-secret-canary' not in evidence and 'key-secret-canary' not in evidence
    assert result.raw_payload['pages'][0]['response']['records'][0]['values'][1] == '***'


@pytest.mark.parametrize('values', ['scalar-secret-canary', {'other': 'mapping-secret-canary'}])
def test_history_raw_masks_malformed_value_containers(values) -> None:
    pages = _pages()
    pages[0]['records'][0]['values'] = values
    result, _ = _fetch(pages=pages)
    assert result.status == 'failed' and result.observations == ()
    assert 'secret-canary' not in json.dumps(result.raw_payload)
    assert result.raw_payload['pages'][0]['response']['records'][0]['values'] == '***'


def test_history_page_callback_receives_safe_incremental_evidence() -> None:
    # 延迟导入使RED落在真实分发行为，不以collection ImportError充数。
    from app.connectors.macro.chinamoney_usdcny import fetch_usdcny_history

    pages = _pages()
    snapshots = []
    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=pages[int(request.url.params['pageNum']) - 1])
    )), max_attempts=1, backoff_seconds=(0,)) as transport:
        result = fetch_usdcny_history(
            transport, MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.fx.usdcny'),
            _source(), _window(), on_page=lambda raw, requests: snapshots.append((deepcopy(raw), requests)),
        )
    assert result.status == 'success'
    assert [len(raw['pages']) for raw, _ in snapshots] == [1, 2]
    assert [requests for _, requests in snapshots] == [1, 2]


@pytest.mark.parametrize('mutation,canary,code', [
    ('array_record', 'row-secret-canary', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('mapping_records', 'container-secret-canary', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('scalar_records', 'scalar-records-secret-canary', 'CHINAMONEY_USDCNY_PAGINATION_INVALID'),
    ('ambiguous_values', '12345678901234567890', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('extra_record_field', '22345678901234567890', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
    ('invalid_date', 'date-field-secret-canary', 'CHINAMONEY_USDCNY_SCHEMA_INVALID'),
])
def test_history_rejected_record_shapes_never_expose_untrusted_values_to_raw_or_callback(
    mutation, canary, code,
) -> None:
    from app.connectors.macro.chinamoney_usdcny import fetch_usdcny_history

    pages = _pages()
    if mutation == 'array_record':
        pages[0]['records'][0] = ['2016-10-31', canary]
    elif mutation == 'mapping_records':
        pages[0]['records'] = {'date': '2016-10-31', 'values': [canary]}
    elif mutation == 'scalar_records':
        pages[0]['records'] = canary
    elif mutation == 'ambiguous_values':
        pages[0]['records'][0]['values'] = [canary, '6.7008']
    elif mutation == 'extra_record_field':
        pages[0]['records'][0].update(values=[canary], other='extra-field-secret-canary')
    elif mutation == 'invalid_date':
        pages[0]['records'][0]['date'] = canary
    snapshots = []
    wire_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire_requests.append(request)
        return httpx.Response(200, json=pages[int(request.url.params['pageNum']) - 1])

    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handler)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        result = fetch_usdcny_history(
            transport, MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.fx.usdcny'),
            _source(), _window(), on_page=lambda raw, requests: snapshots.append((deepcopy(raw), requests)),
        )
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == code
    assert result.request_count == len(wire_requests) == 1
    assert len(result.raw_payload['pages']) == 1 and len(snapshots) == 1
    assert snapshots[0][1] == 1
    for raw in (result.raw_payload, snapshots[0][0]):
        evidence = json.dumps(raw)
        assert canary not in evidence
        assert 'extra-field-secret-canary' not in evidence


def test_unknown_history_api_rejection_masks_source_and_terms_url_credentials_without_http() -> None:
    source = _source().model_copy(update={
        'api': 'CcprHis',
        'source_url': URL + '?token=unknown-api-secret-canary',
        'terms_url': 'https://user:terms-secret-canary@example.test/terms',
    })
    result, requests = _fetch(source=source)
    assert result.status == 'failed' and result.observations == ()
    assert result.error.code == 'CHINAMONEY_IDENTIFIER_INVALID'
    assert result.request_count == 0 and requests == []
    assert 'secret-canary' not in json.dumps(result.raw_payload)
    assert '***' in result.raw_payload['sourceUrl']
    assert '***' in result.raw_payload['termsUrl']
