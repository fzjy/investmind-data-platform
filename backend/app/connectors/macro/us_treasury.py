from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from typing import Protocol
import xml.etree.ElementTree as ET

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderSchemaError,
)
from app.connectors.macro.transport import TextTransportResponse
from app.connectors.macro.url_policy import is_exact_public_url
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


US_TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
_TREASURY_HOST = "home.treasury.gov"
_TREASURY_PATH = "/resource-center/data-chart-center/interest-rates/pages/xml"
_ATOM = "http://www.w3.org/2005/Atom"
_DATA = "http://schemas.microsoft.com/ado/2007/08/dataservices"
_META = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
_EDM_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)


class UsTreasuryTransport(Protocol):
    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> TextTransportResponse: ...


class UsTreasuryMacroProvider:
    provider_id = "us_treasury"

    def __init__(self, transport: UsTreasuryTransport) -> None:
        self._transport = transport

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        try:
            _validate_source(series, source)
        except ProviderError as exc:
            return _failed_result(source, exc)

        request_count = 0
        responses: list[tuple[int, str]] = []
        values: dict[date, Decimal] = {}
        for year in range(window.start.year, window.end.year + 1):
            try:
                response = self._transport.get_text(
                    str(source.source_url),
                    params={
                        "data": "daily_treasury_yield_curve",
                        "field_tdr_date_value": str(year),
                    },
                )
            except ProviderError as exc:
                total_count = request_count + exc.request_count
                return _failed_result(
                    source,
                    exc,
                    request_count=total_count,
                    raw_payload=_audit_payload(source, responses),
                )
            request_count += response.request_count
            responses.append((year, response.text))
            try:
                parsed = _parse_treasury_xml(response.text, source)
            except ProviderError as exc:
                return _failed_result(
                    source,
                    exc,
                    request_count=request_count,
                    raw_payload=_audit_payload(source, responses),
                )
            for observed_at, value in parsed.items():
                existing = values.get(observed_at)
                if existing is not None and existing != value:
                    return _failed_result(
                        source,
                        ProviderSchemaError("US_TREASURY_DUPLICATE_DATE"),
                        request_count=request_count,
                        raw_payload=_audit_payload(source, responses),
                    )
                values[observed_at] = value

        observations = tuple(
            ProviderObservation(
                provider="us_treasury",
                provider_series_id=source.series or "",
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=values[observed_at],
                source_url=str(source.source_url),
            )
            for observed_at in sorted(values)
            if window.start <= observed_at <= window.end
        )
        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload=_audit_payload(source, responses),
            request_count=request_count,
        )


def _validate_source(
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
) -> None:
    if not is_exact_public_url(
        str(source.source_url),
        host=_TREASURY_HOST,
        path=_TREASURY_PATH,
    ):
        raise ProviderSchemaError("US_TREASURY_URL_NOT_ALLOWED")
    if (
        series.id != "us.rate.treasury_10y"
        or source.provider != "us_treasury"
        or source.series != "BC_10YEAR"
        or source.field != "BC_10YEAR"
        or source.transform != "identity"
    ):
        raise ProviderSchemaError("US_TREASURY_IDENTIFIER_INVALID")


def _parse_treasury_xml(
    text: str,
    source: MacroSourceDefinition,
) -> dict[date, Decimal]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ProviderSchemaError("US_TREASURY_XML_INVALID") from exc
    if root.tag != f"{{{_ATOM}}}feed":
        raise ProviderSchemaError("US_TREASURY_XML_INVALID")
    values: dict[date, Decimal] = {}
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        properties = entry.find(
            f"{{{_ATOM}}}content/{{{_META}}}properties"
        )
        if properties is None:
            raise ProviderSchemaError("US_TREASURY_ROW_INVALID")
        date_element = properties.find(f"{{{_DATA}}}NEW_DATE")
        value_element = properties.find(f"{{{_DATA}}}{source.field}")
        if (
            date_element is None
            or value_element is None
            or date_element.text is None
            or value_element.text is None
        ):
            raise ProviderSchemaError("US_TREASURY_ROW_INVALID")
        observed_at = _treasury_date(date_element.text)
        try:
            value = Decimal(value_element.text.strip())
        except InvalidOperation as exc:
            raise ProviderSchemaError("US_TREASURY_VALUE_INVALID") from exc
        if not value.is_finite():
            raise ProviderSchemaError("US_TREASURY_VALUE_INVALID")
        if observed_at in values:
            raise ProviderSchemaError("US_TREASURY_DUPLICATE_DATE")
        values[observed_at] = value
    return values


def _treasury_date(value: str) -> date:
    if _EDM_DATETIME.fullmatch(value) is None:
        raise ProviderSchemaError("US_TREASURY_DATE_INVALID")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ProviderSchemaError("US_TREASURY_DATE_INVALID") from exc


def _audit_payload(
    source: MacroSourceDefinition,
    responses: list[tuple[int, str]],
) -> dict[str, object]:
    return {
        "sourceUrl": str(source.source_url),
        "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
        "responses": [
            {
                "year": year,
                "contentType": "application/atom+xml",
                "contentSha256": sha256(text.encode("utf-8")).hexdigest(),
            }
            for year, text in responses
        ],
    }


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int | None = None,
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    safe_count = error.request_count if request_count is None else request_count
    return ProviderFetchResult(
        provider="us_treasury",
        status="failed",
        observations=(),
        raw_payload=raw_payload or _audit_payload(source, []),
        request_count=safe_count,
        error=error.sanitized_copy(request_count=safe_count),
    )
