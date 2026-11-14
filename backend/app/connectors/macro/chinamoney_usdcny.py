"""官方USD/CNY中间价日频历史；完整分页验证后才返回观测。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol, cast
from urllib.parse import urlencode

from app.connectors.macro.contracts import (
    MacroFetchWindow, ProviderError, ProviderFetchResult, ProviderObservation, ProviderSchemaError,
)
from app.connectors.macro.sanitization import sanitize_macro_text, sanitize_macro_value
from app.connectors.macro.transport import JsonTransportResponse
from app.connectors.macro.url_policy import is_exact_public_url
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_HOST = 'www.chinamoney.com.cn'
_PATH = '/ags/ms/cm-u-bk-ccpr/CcprHisNew'
_CHINA_TIME = timezone(timedelta(hours=8))
_PAGE_SIZE = 10
_PAGINATION_ERROR = 'CHINAMONEY_USDCNY_PAGINATION_INVALID'
_SCHEMA_ERROR = 'CHINAMONEY_USDCNY_SCHEMA_INVALID'


class UsdcnyTransport(Protocol):
    def post_json(self, url: str, payload: Mapping[str, object]) -> JsonTransportResponse: ...


def fetch_usdcny_history(
    transport: UsdcnyTransport, series: MacroSeriesDefinition,
    source: MacroSourceDefinition, window: MacroFetchWindow,
    *, on_page: Callable[[dict[str, object], int], None] | None = None,
) -> ProviderFetchResult:
    pages: list[dict[str, object]] = []
    raw: dict[str, object] = {'sourceUrl': sanitize_macro_text(str(source.source_url)), 'pages': pages}
    requests = 0
    try:
        budget = _validate_contract(series, source, window)
        observations: list[ProviderObservation] = []
        previous_date: date | None = None
        signature: tuple[int, int, tuple[str, ...]] | None = None
        page_number, page_total = 1, 1
        while page_number <= page_total:
            params = {
                'startDate': window.start.isoformat(), 'endDate': window.end.isoformat(),
                'currency': 'USD/CNY', 'pageNum': str(page_number), 'pageSize': str(_PAGE_SIZE),
            }
            try:
                response = transport.post_json(str(source.source_url) + '?' + urlencode(params), {})
            except ProviderError as exc:
                requests += exc.request_count
                raise
            requests += response.request_count
            pages.append({'request': params, 'response': _safe_page(response.payload, window)})
            if on_page is not None:
                on_page(raw, requests)
            total, page_total, currencies, records = _validate_page(response.payload, window, page_number)
            current_signature = total, page_total, currencies
            if signature is not None and signature != current_signature:
                raise ProviderSchemaError(_PAGINATION_ERROR)
            signature = current_signature
            if page_total > budget:
                raise ProviderSchemaError('CHINAMONEY_USDCNY_PAGE_BUDGET_EXCEEDED')
            for record in records:
                observed_at, value = _parse_record(record, window)
                if previous_date is not None and observed_at >= previous_date:
                    raise ProviderSchemaError(_PAGINATION_ERROR)
                previous_date = observed_at
                observations.append(ProviderObservation(
                    provider='chinamoney', provider_series_id='USD/CNY', observed_at=observed_at,
                    released_at=None, revision=None, value=value, source_url=str(source.source_url),
                ))
            page_number += 1
        return ProviderFetchResult(
            provider='chinamoney', status='success' if observations else 'no_change',
            observations=tuple(observations), raw_payload=raw, request_count=requests,
        )
    except ProviderError as exc:
        return ProviderFetchResult(
            provider='chinamoney', status='failed', observations=(), raw_payload=raw,
            request_count=requests, error=ProviderError(
                sanitize_macro_text(exc.code), sanitize_macro_text(exc.message),
                retryable=exc.retryable, request_count=requests,
            ),
        )


def _validate_contract(series: MacroSeriesDefinition, source: MacroSourceDefinition,
                       window: MacroFetchWindow) -> int:
    budget = validate_usdcny_source(series, source)
    if (
        window.as_of.utcoffset() is None or window.start < date(2006, 1, 4)
        or window.start > window.end or (window.end - window.start).days >= 31
        or window.end > window.as_of.astimezone(_CHINA_TIME).date()
    ):
        raise ProviderSchemaError('CHINAMONEY_WINDOW_INVALID')
    return budget


def validate_usdcny_source(series: MacroSeriesDefinition, source: MacroSourceDefinition) -> int:
    """零HTTP验证来源身份和页数上限，供历史队列加载器复用。"""
    if (
        series.id != 'cn.fx.usdcny' or series.region != 'CN' or series.category != 'rates_fx'
        or series.frequency != 'daily' or series.unit != 'CNY per USD'
        or source.provider != 'chinamoney' or source.series != 'USD/CNY'
        or source.api != 'CcprHisNew' or source.field != 'values' or source.transform != 'identity'
        or source.access_mode != 'public_page' or source.response_fields != ('date', 'values')
    ):
        raise ProviderSchemaError('CHINAMONEY_IDENTIFIER_INVALID')
    if not is_exact_public_url(str(source.source_url), host=_HOST, path=_PATH):
        raise ProviderSchemaError('CHINAMONEY_HOST_NOT_ALLOWED')
    if set(source.params) != {'maxPages'} or source.params.get('maxPages') not in ('1', '2', '3', '4'):
        raise ProviderSchemaError('CHINAMONEY_PARAMS_INVALID')
    return int(source.params['maxPages'])


def _validate_page(payload: dict[str, object], window: MacroFetchWindow, page_number: int
                   ) -> tuple[int, int, tuple[str, ...], list[object]]:
    head = payload.get('head')
    if not isinstance(head, Mapping) or (
        head.get('version') != '2.0' or head.get('provider') != 'CWAP' or head.get('rep_code') != '200'
    ):
        raise ProviderSchemaError('CHINAMONEY_HEADER_INVALID')
    data, records = payload.get('data'), payload.get('records')
    if not isinstance(data, Mapping) or not isinstance(records, list):
        raise ProviderSchemaError(_PAGINATION_ERROR)
    currencies = data.get('head')
    if (
        not isinstance(currencies, list) or not currencies
        or any(not isinstance(c, str) or re.fullmatch(r'(?:[0-9]{1,3})?[A-Z]{3}/[A-Z]{3}', c) is None
               for c in currencies)
        or len(set(currencies)) != len(currencies) or currencies.count('USD/CNY') != 1
        or data.get('currency') != 'USD/CNY' or data.get('searchlist') != ['USD/CNY']
    ):
        raise ProviderSchemaError(_PAGINATION_ERROR)
    total, page_total = _integer(data.get('total')), _integer(data.get('pageTotal'))
    if (
        total > (window.end - window.start).days + 1
        or page_total != (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        or _integer(data.get('pageNum')) != page_number
        or _integer(data.get('pageSize')) != _PAGE_SIZE
        or data.get('startDate') != window.start.isoformat() or data.get('endDate') != window.end.isoformat()
        or data.get('flagMessage') != ''
        or len(records) != min(_PAGE_SIZE, max(0, total - (page_number - 1) * _PAGE_SIZE))
    ):
        raise ProviderSchemaError(_PAGINATION_ERROR)
    return total, page_total, tuple(currencies), records


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderSchemaError(_PAGINATION_ERROR)
    return value


def _positive_decimal(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 64 or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is None:
        raise ProviderSchemaError(_SCHEMA_ERROR)
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ProviderSchemaError(_SCHEMA_ERROR) from None
    if not number.is_finite() or number <= 0:
        raise ProviderSchemaError(_SCHEMA_ERROR)
    return number


def _parse_record(record: object, window: MacroFetchWindow) -> tuple[date, Decimal]:
    if not isinstance(record, Mapping) or set(record) != {'date', 'values'}:
        raise ProviderSchemaError(_SCHEMA_ERROR)
    raw_date, values = record.get('date'), record.get('values')
    if not isinstance(raw_date, str) or not isinstance(values, list) or len(values) != 1:
        raise ProviderSchemaError(_SCHEMA_ERROR)
    try:
        observed_at = date.fromisoformat(raw_date)
    except ValueError:
        raise ProviderSchemaError(_SCHEMA_ERROR) from None
    if observed_at.isoformat() != raw_date or not window.start <= observed_at <= window.end:
        raise ProviderSchemaError(_SCHEMA_ERROR)
    return observed_at, _positive_decimal(values[0])


def _safe_page(payload: dict[str, object], window: MacroFetchWindow) -> dict[str, object]:
    safe = cast(dict[str, object], sanitize_macro_value(payload))
    data = payload.get('data')
    known_selection = isinstance(data, Mapping) and data.get('currency') == 'USD/CNY' and data.get('searchlist') == ['USD/CNY']
    original_records = payload.get('records')
    if not isinstance(original_records, list):
        if 'records' in safe:
            safe['records'] = '***'
        return safe
    records = cast(list[object], safe['records'])
    for index, original in enumerate(original_records):
        if not isinstance(original, Mapping):
            records[index] = '***'
            continue
        record = cast(dict[str, object], records[index])
        try:
            # 回调先于页面校验；只允许原记录的完整单列合同，不能仅凭数字形状保留值。
            _parse_record(original, window)
            allowed = known_selection
        except ProviderError:
            allowed = False
        for key, value in record.items():
            if key == 'values' and isinstance(value, list):
                record[key] = value if allowed else ['***'] * len(value)
            elif key not in ('date', 'values') or not allowed:
                record[key] = '***'
    return safe
