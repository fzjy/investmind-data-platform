"""已验证的 DR007 日加权月窗，不以其他利率或最新成交价替代。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.connectors.macro.contracts import (
    MacroFetchWindow, ProviderAuthError, ProviderError, ProviderFetchResult,
    ProviderObservation, ProviderPermissionError, ProviderRateLimitError,
    ProviderSchemaError, ProviderTransientError,
)
from app.connectors.tushare.contracts import Scalar, TushareResponse
from app.connectors.tushare.errors import (
    TushareAuthError, TushareError, TusharePermissionError, TushareRateLimitError,
    TushareSchemaError,
)
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition

FIELDS = ('trade_date', 'ts_code', 'repo_maturity', 'weight')
SOURCE_URL = 'https://tushare.pro/document/2?doc_id=256'
CHINA_TIME = timezone(timedelta(hours=8))


class Dr007Client(Protocol):
    def call(self, api_name: str, params: Mapping[str, Scalar],
             fields: Sequence[str]) -> TushareResponse: ...


def validate_dr007_source(series: MacroSeriesDefinition, source: MacroSourceDefinition) -> None:
    if (series.id != 'cn.rate.dr007' or series.region != 'CN' or series.unit != '%'
            or series.frequency != 'daily' or source.provider != 'tushare'
            or source.series != 'DR007.IB' or source.api != 'repo_daily'
            or source.field != 'weight' or source.transform != 'identity'
            or source.access_mode != 'api' or str(source.source_url) != SOURCE_URL
            or source.params != {'ts_code': 'DR007.IB'} or source.response_fields != FIELDS):
        raise ProviderSchemaError('DR007_CONTRACT_INVALID')


def fetch_dr007_history(client: Dr007Client, series: MacroSeriesDefinition,
                        source: MacroSourceDefinition, window: MacroFetchWindow) -> ProviderFetchResult:
    pages: list[dict[str, object]] = []
    raw: dict[str, object] = {'sourceUrl': str(source.source_url), 'pages': pages}
    count = 0
    try:
        validate_dr007_source(series, source)
        if (window.as_of.utcoffset() is None or window.start > window.end
                or (window.end-window.start).days >= 31
                or window.end > window.as_of.astimezone(CHINA_TIME).date()):
            raise ProviderSchemaError('DR007_WINDOW_INVALID')
        params = {**source.params, 'start_date': window.start.strftime('%Y%m%d'),
            'end_date': window.end.strftime('%Y%m%d')}
        count = 1
        response = client.call('repo_daily', params, FIELDS)
        evidence = response.model_dump(mode='json')
        # fields/items 是位置数组，通用按键脱敏无法对应额外秘密列。
        # 保留字段布局/请求证据，但未请求列的值在进入失败审计前一律遮蔽。
        evidence['items'] = [[value if index < len(response.fields)
            and response.fields[index] in FIELDS else '***'
            for index, value in enumerate(row)] for row in response.items]
        pages.append({'request': params, 'response': evidence})
        if response.fields != FIELDS or len(response.items) > (window.end-window.start).days+1:
            raise ProviderSchemaError('DR007_RESPONSE_INVALID')
        observations: list[ProviderObservation] = []
        seen: set[date] = set()
        for row in response.rows():
            if row.get('ts_code') != 'DR007.IB' or row.get('repo_maturity') != 'DR007':
                raise ProviderSchemaError('DR007_IDENTITY_INVALID')
            raw_date, raw_value = row.get('trade_date'), row.get('weight')
            if (not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isascii()
                    or not raw_date.isdigit() or isinstance(raw_value, bool) or raw_value is None):
                raise ProviderSchemaError('DR007_RESPONSE_INVALID')
            try:
                observed = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
                value = Decimal(str(raw_value))
            except (ValueError, InvalidOperation):
                raise ProviderSchemaError('DR007_RESPONSE_INVALID') from None
            if not window.start <= observed <= window.end or not value.is_finite() or observed in seen:
                raise ProviderSchemaError('DR007_RESPONSE_INVALID')
            seen.add(observed)
            observations.append(ProviderObservation('tushare', 'DR007.IB', observed, None,
                None, value, str(source.source_url)))
        return ProviderFetchResult('tushare', 'success' if observations else 'no_change',
            tuple(observations), raw, count)
    except (ProviderError, TushareError, ValueError) as exc:
        if isinstance(exc, ProviderError):
            error = exc
            if error.code == 'MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED':
                count = 0
        else:
            kind = (ProviderAuthError if isinstance(exc, TushareAuthError) else
                ProviderPermissionError if isinstance(exc, TusharePermissionError) else
                ProviderRateLimitError if isinstance(exc, TushareRateLimitError) else
                ProviderSchemaError if isinstance(exc, (TushareSchemaError, ValueError)) else ProviderTransientError)
            code = {ProviderAuthError:'TUSHARE_AUTH_FAILED', ProviderPermissionError:'TUSHARE_PERMISSION_DENIED',
                ProviderRateLimitError:'TUSHARE_RATE_LIMITED', ProviderSchemaError:'DR007_RESPONSE_INVALID',
                ProviderTransientError:'TUSHARE_TRANSIENT_FAILURE'}[kind]
            error = kind(code)
        return ProviderFetchResult('tushare', 'failed', (), raw, count,
            error.sanitized_copy(request_count=count))
