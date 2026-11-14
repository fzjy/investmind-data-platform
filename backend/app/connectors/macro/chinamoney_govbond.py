from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol
from urllib.parse import urlencode

from app.connectors.macro.contracts import (
    MacroFetchWindow, ProviderError, ProviderFetchResult, ProviderObservation, ProviderSchemaError,
)
from app.connectors.macro.transport import JsonTransportResponse
from app.connectors.macro.url_policy import is_exact_public_url
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_HOST = "www.chinamoney.com.cn"
_PATH = "/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis"
_PAGE_SIZE = 15
_CHINA_TIME = timezone(timedelta(hours=8))


class GovbondTransport(Protocol):
    def post_json(self, url: str, payload: Mapping[str, object]) -> JsonTransportResponse: ...


def fetch_govbond_history(
    transport: GovbondTransport, series: MacroSeriesDefinition,
    source: MacroSourceDefinition, window: MacroFetchWindow,
    *, on_page: Callable[[dict[str, object], int], None] | None = None,
) -> ProviderFetchResult:
    pages: list[dict[str, object]] = []
    requests = 0
    raw: dict[str, object] = {"sourceUrl": str(source.source_url), "pages": pages}
    try:
        max_pages = _validate_contract(series, source, window)
        observations: list[ProviderObservation] = []
        seen: set[date] = set()
        expected_counts: tuple[int, int] | None = None
        page_number = 1
        page_total = 1
        while page_number <= page_total:
            params = {
                "lang": "CN", "startDate": window.start.isoformat(),
                "endDate": window.end.isoformat(), "pageNum": str(page_number),
                "pageSize": str(_PAGE_SIZE),
            }
            try:
                response = transport.post_json(str(source.source_url) + "?" + urlencode(params), {})
            except ProviderError as exc:
                requests += exc.request_count
                raise
            requests += response.request_count
            pages.append({"request": params, "response": response.payload})
            if on_page is not None:
                on_page(raw, requests)
            total, page_total, records = _validate_page(response.payload, window, page_number)
            if expected_counts is not None and expected_counts != (total, page_total):
                raise ProviderSchemaError("CHINAMONEY_PAGINATION_INVALID")
            expected_counts = total, page_total
            if page_total > max_pages:
                raise ProviderSchemaError("CHINAMONEY_PAGE_BUDGET_EXCEEDED")
            for record in records:
                observed_at, value = _parse_record(record, window)
                if observed_at in seen:
                    raise ProviderSchemaError("CHINAMONEY_PAGINATION_INVALID")
                seen.add(observed_at)
                observations.append(ProviderObservation(
                    provider="chinamoney", provider_series_id="CGB-10Y",
                    observed_at=observed_at, released_at=None, revision=None,
                    value=value, source_url=str(source.source_url),
                ))
            page_number += 1
        return ProviderFetchResult(
            provider="chinamoney", status="success" if observations else "no_change",
            observations=tuple(observations), raw_payload=raw, request_count=requests,
        )
    except ProviderError as exc:
        return ProviderFetchResult(
            provider="chinamoney", status="failed", observations=(), raw_payload=raw,
            request_count=requests, error=exc.sanitized_copy(request_count=requests),
        )


def _validate_contract(
    series: MacroSeriesDefinition, source: MacroSourceDefinition, window: MacroFetchWindow,
) -> int:
    if (
        series.id != "cn.rate.govbond_10y" or series.region != "CN"
        or series.frequency != "daily" or series.unit != "%"
        or source.provider != "chinamoney" or source.series != "CGB-10Y"
        or source.api != "SddsIntrRateGovYldHis" or source.field != "tenRate"
        or source.transform != "identity" or source.access_mode != "public_page"
        or source.response_fields != ("dateString", "tenRate")
    ):
        raise ProviderSchemaError("CHINAMONEY_IDENTIFIER_INVALID")
    if not is_exact_public_url(str(source.source_url), host=_HOST, path=_PATH):
        raise ProviderSchemaError("CHINAMONEY_HOST_NOT_ALLOWED")
    raw_budget = source.params.get("maxPages", "")
    if (
        set(source.params) != {"maxPages"}
        or re.fullmatch(r"[1-9][0-9]{0,2}", raw_budget) is None
        or int(raw_budget) > 240
    ):
        raise ProviderSchemaError("CHINAMONEY_PARAMS_INVALID")
    if (
        window.as_of.utcoffset() is None or window.start > window.end
        or window.end > window.as_of.astimezone(_CHINA_TIME).date()
    ):
        raise ProviderSchemaError("CHINAMONEY_WINDOW_INVALID")
    return int(raw_budget)


def _validate_page(
    payload: dict[str, object], window: MacroFetchWindow, page_number: int,
) -> tuple[int, int, list[object]]:
    head = payload.get("head")
    if not isinstance(head, Mapping) or (
        head.get("version") != "2.0" or head.get("provider") != "CWAP"
        or head.get("rep_code") != "200"
    ):
        raise ProviderSchemaError("CHINAMONEY_HEADER_INVALID")
    data = payload.get("data")
    records = payload.get("records")
    if not isinstance(data, Mapping) or not isinstance(records, list):
        raise ProviderSchemaError("CHINAMONEY_PAGINATION_INVALID")
    total = _integer(data.get("total"))
    page_total = _integer(data.get("pageTotal"))
    page_index = _integer(data.get("pageNum"))
    page_size = _integer(data.get("pageSize"))
    expected_rows = min(_PAGE_SIZE, max(0, total - (page_number - 1) * _PAGE_SIZE))
    if (
        page_total != (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        or page_index != page_number - 1 or page_size != _PAGE_SIZE
        or data.get("startDateCN") != window.start.isoformat()
        or data.get("endDateCN") != window.end.isoformat()
        or data.get("flagMessage") != "" or len(records) != expected_rows
    ):
        raise ProviderSchemaError("CHINAMONEY_PAGINATION_INVALID")
    return total, page_total, records


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderSchemaError("CHINAMONEY_PAGINATION_INVALID")
    return value


def _parse_record(record: object, window: MacroFetchWindow) -> tuple[date, Decimal]:
    if not isinstance(record, Mapping):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")
    raw_date = record.get("dateString")
    raw_value = record.get("tenRate")
    if not isinstance(raw_date, str) or not isinstance(raw_value, str):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")
    try:
        observed_at = date.fromisoformat(raw_date)
        value = Decimal(raw_value)
    except (ValueError, InvalidOperation) as exc:
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID") from exc
    if (
        observed_at.isoformat() != raw_date or not window.start <= observed_at <= window.end
        or not value.is_finite()
    ):
        raise ProviderSchemaError("CHINAMONEY_SCHEMA_INVALID")
    return observed_at, value
