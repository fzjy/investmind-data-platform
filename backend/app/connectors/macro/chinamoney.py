from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlsplit

from app.connectors.macro.chinamoney_govbond import GovbondTransport, fetch_govbond_history
from app.connectors.macro.chinamoney_usdcny import fetch_usdcny_history
from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderSchemaError,
)
from app.connectors.macro.sanitization import sanitize_macro_text
from app.connectors.macro.transport import JsonTransportResponse
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_ALLOWED_HOSTS = frozenset({"www.chinamoney.com.cn"})
_EXPECTED_PATH = "/r/cms/www/chinamoney/data/fx/ccpr.json"
_EXPECTED_CANONICAL_SERIES = "cn.fx.usdcny"
_EXPECTED_SERIES = "USD/CNY"
_EXPECTED_FIELD = "price"
_EXPECTED_RESPONSE_FIELDS = ("lastDate", "vrtCode", "vrtName", "vrtEName", "price")
_CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


class ChinaMoneyJsonTransport(GovbondTransport, Protocol):
    def get_json(self, url: str) -> JsonTransportResponse: ...


class ChinaMoneyMacroProvider:
    provider_id = "chinamoney"

    def __init__(self, transport: ChinaMoneyJsonTransport) -> None:
        self._transport = transport

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        if source.api == 'CcprHisNew':
            return fetch_usdcny_history(self._transport, series, source, window)
        if series.id == "cn.rate.govbond_10y":
            return fetch_govbond_history(self._transport, series, source, window)
        if source.api is not None:
            return _failed_result(source, ProviderSchemaError('CHINAMONEY_IDENTIFIER_INVALID'), request_count=0)
        contract_error = _validate_source(series, source)
        if contract_error is not None:
            return _failed_result(source, contract_error, request_count=0)
        try:
            response = self._transport.get_json(str(source.source_url))
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=exc.request_count)
        try:
            observation = _parse_observation(source, response.payload)
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=response.request_count)

        observations = (
            (observation,)
            if window.start <= observation.observed_at <= window.end
            else ()
        )
        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload={
                "sourceUrl": str(source.source_url),
                "lastDate": observation.observed_at.isoformat(),
                "series": observation.provider_series_id,
            },
            request_count=response.request_count,
        )


def _validate_source(
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
) -> ProviderSchemaError | None:
    parsed_url = urlsplit(str(source.source_url))
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in _ALLOWED_HOSTS
        or parsed_url.port is not None
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        return ProviderSchemaError("CHINAMONEY_HOST_NOT_ALLOWED")
    if parsed_url.path != _EXPECTED_PATH:
        return ProviderSchemaError("CHINAMONEY_HOST_NOT_ALLOWED")
    if (
        series.id != _EXPECTED_CANONICAL_SERIES
        or source.provider != "chinamoney"
        or source.series != _EXPECTED_SERIES
        or source.field != _EXPECTED_FIELD
        or source.response_fields != _EXPECTED_RESPONSE_FIELDS
        or source.access_mode != "public_page"
    ):
        return ProviderSchemaError("CHINAMONEY_IDENTIFIER_INVALID")
    return None


def _parse_observation(
    source: MacroSourceDefinition,
    payload: dict[str, object],
) -> ProviderObservation:
    head = payload.get("head")
    if not isinstance(head, Mapping):
        raise ProviderSchemaError("CHINAMONEY_HEADER_INVALID")
    if (
        head.get("version") != "2.0"
        or head.get("provider") != "CWAP"
        or head.get("rep_code") != "200"
    ):
        raise ProviderSchemaError("CHINAMONEY_HEADER_INVALID")

    data = payload.get("data")
    records = payload.get("records")
    if not isinstance(data, Mapping) or not isinstance(records, list):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")
    last_date = data.get("lastDate")
    if not isinstance(last_date, str):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")

    matching_records = [record for record in records if _is_usdcny_record(record)]
    if len(matching_records) != 1:
        raise ProviderSchemaError("CHINAMONEY_IDENTIFIER_INVALID")
    record = matching_records[0]
    raw_price = record.get("price")
    if not isinstance(raw_price, str):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")
    try:
        value = Decimal(raw_price)
    except InvalidOperation as exc:
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID") from exc
    observed_at, released_at = _parse_last_date(last_date)
    return ProviderObservation(
        provider="chinamoney",
        provider_series_id=_EXPECTED_SERIES,
        observed_at=observed_at,
        released_at=released_at,
        revision=None,
        value=value,
        source_url=str(source.source_url),
    )


def _is_usdcny_record(value: object) -> bool:
    return isinstance(value, Mapping) and (
        value.get("vrtCode") == "1"
        and value.get("vrtName") == "美元/人民币"
        and value.get("vrtEName") == "USD/CNY"
        and value.get("foreignCName") == "USD"
    )


def _parse_last_date(raw_value: str) -> tuple[date, datetime | None]:
    try:
        if len(raw_value) == 10:
            return date.fromisoformat(raw_value), None
        local_time = datetime.strptime(raw_value, "%Y-%m-%d %H:%M").replace(
            tzinfo=_CHINA_STANDARD_TIME
        )
    except ValueError as exc:
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID") from exc
    return local_time.date(), local_time.astimezone(UTC)


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="chinamoney",
        status="failed",
        observations=(),
        raw_payload={
            "sourceUrl": sanitize_macro_text(str(source.source_url)),
            "termsUrl": sanitize_macro_text(str(source.terms_url)) if source.terms_url is not None else None,
        },
        request_count=request_count,
        error=error.sanitized_copy(request_count=request_count),
    )
