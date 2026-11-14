from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderSchemaError,
)
from app.connectors.macro.transport import JsonTransportResponse
from app.connectors.macro.url_policy import is_exact_public_url
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


NYFED_EFFR_URL = (
    "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
)
_NYFED_HOST = "markets.newyorkfed.org"
_NYFED_PATH = "/api/rates/unsecured/effr/search.json"


class NyFedTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse: ...


class NyFedMacroProvider:
    provider_id = "nyfed"

    def __init__(self, transport: NyFedTransport) -> None:
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

        try:
            response = self._transport.get_json(
                str(source.source_url),
                params={
                    "startDate": window.start.isoformat(),
                    "endDate": window.end.isoformat(),
                    "type": "rate",
                },
            )
        except ProviderError as exc:
            return _failed_result(source, exc)

        audit = {
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
            "payload": response.payload,
        }
        try:
            observations = _observations(source, response.payload, window)
        except ProviderError as exc:
            return _failed_result(
                source,
                exc,
                request_count=response.request_count,
                raw_payload=audit,
            )
        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload=audit,
            request_count=response.request_count,
        )


def _validate_source(
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
) -> None:
    if not is_exact_public_url(
        str(source.source_url),
        host=_NYFED_HOST,
        path=_NYFED_PATH,
    ):
        raise ProviderSchemaError("NYFED_URL_NOT_ALLOWED")
    if (
        series.id != "us.rate.effective_fed_funds"
        or source.provider != "nyfed"
        or source.series != "EFFR"
        or source.field != "percent"
        or source.transform != "identity"
    ):
        raise ProviderSchemaError("NYFED_IDENTIFIER_INVALID")


def _observations(
    source: MacroSourceDefinition,
    payload: Mapping[str, object],
    window: MacroFetchWindow,
) -> tuple[ProviderObservation, ...]:
    raw_rates = payload.get("refRates")
    if not isinstance(raw_rates, list):
        raise ProviderSchemaError("NYFED_RATES_MISSING")
    observations: list[ProviderObservation] = []
    seen_dates: set[date] = set()
    for raw_rate in raw_rates:
        if not isinstance(raw_rate, Mapping) or raw_rate.get("type") != source.series:
            raise ProviderSchemaError("NYFED_RATE_INVALID")
        observed_at = _required_date(raw_rate.get("effectiveDate"))
        if observed_at in seen_dates:
            raise ProviderSchemaError("NYFED_DUPLICATE_DATE")
        seen_dates.add(observed_at)
        if observed_at < window.start or observed_at > window.end:
            continue
        value = _required_decimal(raw_rate.get(source.field))
        raw_revision = raw_rate.get("revisionIndicator")
        if raw_revision is not None and not isinstance(raw_revision, str):
            raise ProviderSchemaError("NYFED_REVISION_INVALID")
        observations.append(
            ProviderObservation(
                provider="nyfed",
                provider_series_id=source.series or "",
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=value,
                source_url=str(source.source_url),
            )
        )
    return tuple(sorted(observations, key=lambda item: item.observed_at))


def _required_date(value: object) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError("NYFED_DATE_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError("NYFED_DATE_INVALID") from exc


def _required_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ProviderSchemaError("NYFED_VALUE_INVALID")
    try:
        decimal_value = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ProviderSchemaError("NYFED_VALUE_INVALID") from exc
    if not decimal_value.is_finite():
        raise ProviderSchemaError("NYFED_VALUE_INVALID")
    return decimal_value


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int | None = None,
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    safe_count = error.request_count if request_count is None else request_count
    return ProviderFetchResult(
        provider="nyfed",
        status="failed",
        observations=(),
        raw_payload=raw_payload
        or {
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
        },
        request_count=safe_count,
        error=error.sanitized_copy(request_count=safe_count),
    )
