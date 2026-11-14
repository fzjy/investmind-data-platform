from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderAuthError,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTransientError,
)
from app.connectors.tushare.contracts import Scalar, TushareResponse
from app.connectors.tushare.errors import (
    TushareAuthError,
    TushareError,
    TusharePermissionError,
    TushareRateLimitError,
    TushareSchemaError,
    TushareTransientError,
)
from app.connectors.macro.tushare_dr007 import fetch_dr007_history
from app.connectors.macro.tushare_dr007_current import fetch_dr007_current
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_SECRET_KEY_PARTS = frozenset({"apikey", "token", "authorization", "password"})


class TushareMacroClient(Protocol):
    def call(
        self,
        api_name: str,
        params: Mapping[str, Scalar],
        fields: Sequence[str],
    ) -> TushareResponse: ...


class TushareMacroProvider:
    provider_id = "tushare"

    def __init__(self, client: TushareMacroClient) -> None:
        self._client = client

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        if series.id == 'cn.rate.dr007':
            if 'maxPages' in source.params:
                return fetch_dr007_current(self._client, series, source, window)
            return fetch_dr007_history(self._client, series, source, window)
        if source.api is None:
            return _failed_result(source, ProviderSchemaError("TUSHARE_API_MISSING"))
        request_count = 0
        rows: list[Mapping[str, Scalar]] = []
        raw_rows: list[dict[str, object]] = []
        requests: list[dict[str, object]] = []
        raw_payload: dict[str, object] = {"rows": raw_rows, "requests": requests}
        part_audit: dict[str, object] | None = None
        try:
            _tushare_window_params(series.frequency, source, window)
            windows = [window]
            if series.frequency == "daily":
                windows = []
                start = window.start
                while start <= window.end:
                    end = min(start + timedelta(days=364), window.end)
                    windows.append(MacroFetchWindow(start, end, window.as_of))
                    start = end + timedelta(days=1)
            for part in windows:
                params = _tushare_window_params(series.frequency, source, part)
                part_audit = {
                    "apiName": source.api,
                    "field": source.field,
                    "params": params,
                    "responseFields": list(source.response_fields),
                    "sourceUrl": str(source.source_url),
                    "window": {
                        "start": part.start.isoformat(),
                        "end": part.end.isoformat(),
                        "asOf": part.as_of.isoformat(),
                    },
                    "status": "pending",
                    "rows": [],
                }
                requests.append(part_audit)
                request_count += 1
                response = self._client.call(source.api, params, source.response_fields)
                part_rows = response.rows()
                safe_rows = [_sanitize_row(row, source.response_fields) for row in part_rows]
                part_audit.update(status="success", rows=safe_rows)
                raw_rows.extend(safe_rows)
                rows.extend(part_rows)
            observations = _observations(series, source, rows, window)
        except (TushareError, ProviderError) as exc:
            error = _macro_error(exc) if isinstance(exc, TushareError) else exc
            # 成功仅表示已收到可解码响应；观测校验失败仍保留全部正文。
            if part_audit is not None and part_audit["status"] == "pending":
                part_audit.update(status="failed", errorCode=error.code)
            return _failed_result(
                source, error, request_count=request_count, raw_payload=raw_payload,
            )

        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload=raw_payload,
            request_count=request_count,
        )


def _tushare_window_params(
    frequency: str,
    source: MacroSourceDefinition,
    window: MacroFetchWindow,
) -> dict[str, str]:
    api = source.api
    supported = {"cn_gdp": "quarterly", "cn_cpi": "monthly", "cn_ppi": "monthly", "cn_pmi": "monthly", "cn_m": "monthly", "us_tycr": "daily", "fx_daily": "daily", "yc_cb": "daily"}
    if api not in supported or supported[api] != frequency:
        raise ProviderSchemaError("TUSHARE_CONTRACT_UNVERIFIED")
    selectors = {"fx_daily": {"ts_code", "exchange"}, "yc_cb": {"ts_code", "curve_type", "curve_term"}}.get(api, set())
    if set(source.params) != selectors or any(not value.strip() for value in source.params.values()):
        raise ProviderSchemaError("TUSHARE_SELECTORS_INVALID")
    if not selectors <= set(source.response_fields):
        raise ProviderSchemaError("TUSHARE_IDENTITY_FIELDS_MISSING")
    if frequency == "monthly":
        return {"start_m": window.start.strftime("%Y%m"), "end_m": window.end.strftime("%Y%m")}
    if frequency == "quarterly":
        return {"start_q": f"{window.start.year}Q{(window.start.month - 1) // 3 + 1}", "end_q": f"{window.end.year}Q{(window.end.month - 1) // 3 + 1}"}
    return {**source.params,
        "start_date": window.start.strftime("%Y%m%d"),
        "end_date": window.end.strftime("%Y%m%d"),
    }


def _observations(
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
    rows: Sequence[Mapping[str, Scalar]],
    window: MacroFetchWindow,
) -> tuple[ProviderObservation, ...]:
    if source.api is None:
        raise ProviderSchemaError("TUSHARE_API_MISSING")
    if not source.response_fields:
        raise ProviderSchemaError("TUSHARE_DATE_FIELD_MISSING")
    date_field = source.response_fields[0]
    observations: list[ProviderObservation] = []
    seen: dict[date, Decimal] = {}
    for row in rows:
        for key, expected in source.params.items():
            actual = row.get(key)
            equal = str(actual) == expected
            if key == "curve_term":
                equal = actual is not None and _decimal_value(actual) == Decimal(expected)
            if not equal:
                raise ProviderSchemaError("TUSHARE_SERIES_IDENTITY_MISMATCH")
        value = row.get(source.field)
        if value is None or value == "" or value == ".":
            continue
        decimal_value = _decimal_value(value)
        observed_at = _parse_observed_at(row.get(date_field), series.frequency)
        if observed_at < window.start or observed_at > window.end:
            continue
        if observed_at in seen:
            if seen[observed_at] != decimal_value:
                raise ProviderSchemaError("TUSHARE_MIXED_SERIES")
            continue
        seen[observed_at] = decimal_value
        observations.append(
            ProviderObservation(
                provider="tushare",
                provider_series_id=source.api + ":" + source.field + "".join(f":{key}={value}" for key, value in sorted(source.params.items())),
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=decimal_value,
                source_url=str(source.source_url),
            )
        )
    return tuple(observations)


def _decimal_value(value: Scalar) -> Decimal:
    if isinstance(value, bool):
        raise ProviderSchemaError("TUSHARE_VALUE_INVALID")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderSchemaError("TUSHARE_VALUE_INVALID") from exc


def _parse_observed_at(value: Scalar, frequency: str) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError("TUSHARE_DATE_INVALID")
    try:
        if len(value) == 8 and value.isdigit():
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
        if len(value) == 6 and value.isdigit():
            return date(int(value[0:4]), int(value[4:6]), 1)
        if len(value) == 6 and value[4] in {"Q", "q"} and value[5] in "1234":
            return date(int(value[0:4]), (int(value[5]) - 1) * 3 + 1, 1)
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError("TUSHARE_DATE_INVALID") from exc
    if frequency == "monthly" and parsed.day != 1:
        return date(parsed.year, parsed.month, 1)
    if frequency == "quarterly":
        return date(parsed.year, ((parsed.month - 1) // 3) * 3 + 1, 1)
    return parsed


def _sanitize_row(row: Mapping[str, Scalar], fields: Sequence[str]) -> dict[str, object]:
    return {
        field: value
        for field, value in row.items()
        if field in fields and not _is_secret_key(field)
    }


def _is_secret_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _macro_error(error: TushareError) -> ProviderError:
    if isinstance(error, TushareAuthError):
        return ProviderAuthError("TUSHARE_AUTH_FAILED")
    if isinstance(error, TusharePermissionError):
        return ProviderPermissionError("TUSHARE_PERMISSION_DENIED")
    if isinstance(error, TushareRateLimitError):
        return ProviderRateLimitError("TUSHARE_RATE_LIMITED")
    if isinstance(error, TushareTransientError):
        return ProviderTransientError("TUSHARE_TRANSIENT_FAILURE")
    if isinstance(error, TushareSchemaError):
        return ProviderSchemaError("TUSHARE_SCHEMA_INVALID")
    return ProviderSchemaError("TUSHARE_RESPONSE_INVALID")


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int = 0,
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="tushare",
        status="failed",
        observations=(),
        raw_payload=raw_payload if raw_payload is not None else {
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url),
        },
        request_count=request_count,
        error=error.sanitized_copy(request_count=request_count),
    )
