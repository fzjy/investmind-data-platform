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


BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
_BLS_HOST = "api.bls.gov"
_BLS_PATH = "/publicAPI/v1/timeseries/data/"
_SUPPORTED_SERIES = {
    "us.cpi.yoy": ("CUSR0000SA0", "pc1"),
    "us.cpi.core_yoy": ("CUSR0000SA0L1E", "pc1"),
    "us.labor.unemployment_rate": ("LNS14000000", "identity"),
    "us.labor.nonfarm_payroll_change": ("CES0000000001", "chg"),
}


class BlsTransport(Protocol):
    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse: ...


class BlsMacroProvider:
    provider_id = "bls"

    def __init__(self, transport: BlsTransport) -> None:
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

        warmup_year = (
            window.start.year - 1
            if source.transform in {"pc1", "chg"}
            else window.start.year
        )
        payloads: list[Mapping[str, object]] = []
        request_count = 0
        for start_year, end_year in _bls_year_chunks(
            warmup_year,
            window.end.year,
            limit=10,
        ):
            try:
                response = self._transport.post_json(
                    str(source.source_url),
                    {
                        "seriesid": [source.series],
                        "startyear": str(start_year),
                        "endyear": str(end_year),
                    },
                )
            except ProviderError as exc:
                total_count = request_count + exc.request_count
                return _failed_result(
                    source,
                    exc,
                    request_count=total_count,
                    raw_payload=_audit_payload(source, payloads),
                )
            request_count += response.request_count
            payloads.append(response.payload)

        try:
            levels = _bls_rows(payloads, source)
            observations = _transform_and_trim(levels, series, source, window)
        except ProviderError as exc:
            return _failed_result(
                source,
                exc,
                request_count=request_count,
                raw_payload=_audit_payload(source, payloads),
            )

        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload=_audit_payload(source, payloads, observations),
            request_count=request_count,
        )


def _validate_source(
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
) -> None:
    if not is_exact_public_url(
        str(source.source_url),
        host=_BLS_HOST,
        path=_BLS_PATH,
    ):
        raise ProviderSchemaError("BLS_URL_NOT_ALLOWED")
    expected = _SUPPORTED_SERIES.get(series.id)
    if (expected is None or source.provider != "bls" or source.series != expected[0]
            or source.api != "publicAPI/v1/timeseries/data" or source.params):
        raise ProviderSchemaError("BLS_IDENTIFIER_INVALID")
    if source.field == "value" and source.transform == expected[1]:
        return
    raise ProviderSchemaError("BLS_IDENTIFIER_INVALID")


def _bls_year_chunks(
    start_year: int,
    end_year: int,
    *,
    limit: int,
) -> tuple[tuple[int, int], ...]:
    if limit < 1 or end_year < start_year:
        raise ProviderSchemaError("BLS_YEAR_WINDOW_INVALID")
    chunks: list[tuple[int, int]] = []
    cursor = start_year
    while cursor <= end_year:
        chunk_end = min(cursor + limit - 1, end_year)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + 1
    return tuple(chunks)


def _bls_rows(
    payloads: list[Mapping[str, object]],
    source: MacroSourceDefinition,
) -> dict[date, Decimal]:
    if source.series is None:
        raise ProviderSchemaError("BLS_IDENTIFIER_INVALID")
    rows: dict[date, Decimal] = {}
    for payload in payloads:
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise ProviderSchemaError("BLS_REQUEST_REJECTED")
        results = payload.get("Results")
        wrappers = [results] if isinstance(results, Mapping) else results
        if not isinstance(wrappers, list) or not wrappers:
            raise ProviderSchemaError("BLS_RESULTS_MISSING")
        raw_series = []
        for wrapper in wrappers:
            if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("series"), list):
                raise ProviderSchemaError("BLS_SERIES_MISSING")
            raw_series.extend(wrapper["series"])
        if any(not isinstance(item, Mapping) or not isinstance(item.get("seriesID"), str)
               or not isinstance(item.get("data"), list) for item in raw_series):
            raise ProviderSchemaError("BLS_SERIES_MISSING")
        matches = [
            item
            for item in raw_series
            if isinstance(item, Mapping) and item.get("seriesID") == source.series
        ]
        if len(matches) != 1:
            raise ProviderSchemaError("BLS_SERIES_MISSING")
        raw_rows = matches[0].get("data")
        if not isinstance(raw_rows, list):
            raise ProviderSchemaError("BLS_DATA_MISSING")
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise ProviderSchemaError("BLS_ROW_INVALID")
            observed_at = _bls_period(raw_row)
            if observed_at is None:
                continue
            value = _decimal_field(raw_row, source.field)
            previous = rows.get(observed_at)
            if previous is not None and previous != value:
                raise ProviderSchemaError("BLS_DUPLICATE_PERIOD_CONFLICT")
            rows[observed_at] = value
    return rows


def _bls_period(row: Mapping[str, object]) -> date | None:
    year = row.get("year")
    period = row.get("period")
    if not isinstance(year, str) or not isinstance(period, str):
        raise ProviderSchemaError("BLS_PERIOD_INVALID")
    if period == "M13":
        return None
    if len(period) != 3 or not period.startswith("M") or not period[1:].isdigit():
        raise ProviderSchemaError("BLS_PERIOD_INVALID")
    month = int(period[1:])
    if month < 1 or month > 12 or len(year) != 4 or not year.isdigit():
        raise ProviderSchemaError("BLS_PERIOD_INVALID")
    try:
        return date(int(year), month, 1)
    except ValueError as exc:
        raise ProviderSchemaError("BLS_PERIOD_INVALID") from exc


def _decimal_field(row: Mapping[str, object], field: str) -> Decimal:
    current: object = row
    for part in field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise ProviderSchemaError("BLS_VALUE_MISSING")
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, (str, int, float)):
        raise ProviderSchemaError("BLS_VALUE_INVALID")
    try:
        value = Decimal(str(current).strip())
    except InvalidOperation as exc:
        raise ProviderSchemaError("BLS_VALUE_INVALID") from exc
    if not value.is_finite():
        raise ProviderSchemaError("BLS_VALUE_INVALID")
    return value


def _transform_and_trim(
    values: Mapping[date, Decimal],
    series: MacroSeriesDefinition,
    source: MacroSourceDefinition,
    window: MacroFetchWindow,
) -> tuple[ProviderObservation, ...]:
    observations: list[ProviderObservation] = []
    for observed_at in sorted(values):
        if observed_at < window.start or observed_at > window.end:
            continue
        value = values[observed_at]
        if source.transform == "pc1":
            previous = values.get(date(observed_at.year - 1, observed_at.month, 1))
            if previous is None:
                continue
            if previous == 0:
                raise ProviderSchemaError("BLS_TRANSFORM_ZERO_BASE")
            value = ((value / previous) - Decimal(1)) * Decimal(100)
        elif source.transform == "chg":
            previous_month = (
                date(observed_at.year - 1, 12, 1)
                if observed_at.month == 1
                else date(observed_at.year, observed_at.month - 1, 1)
            )
            previous = values.get(previous_month)
            if previous is None:
                continue
            value = value - previous
        elif source.transform != "identity":
            raise ProviderSchemaError("BLS_TRANSFORM_UNSUPPORTED")
        observations.append(
            ProviderObservation(
                provider="bls",
                provider_series_id=source.series or "",
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=value,
                source_url=str(source.source_url),
            )
        )
    return tuple(observations)


def _audit_payload(
    source: MacroSourceDefinition,
    payloads: list[Mapping[str, object]],
    observations: tuple[ProviderObservation, ...] = (),
) -> dict[str, object]:
    return {
        "sourceUrl": str(source.source_url),
        "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
        "responses": [dict(payload) for payload in payloads],
        "normalizedObservations": [{"observedAt": row.observed_at.isoformat(),
            "value": str(row.value), "sourceUrl": row.source_url} for row in observations],
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
        provider="bls",
        status="failed",
        observations=(),
        raw_payload=raw_payload or _audit_payload(source, []),
        request_count=safe_count,
        error=error.sanitized_copy(request_count=safe_count),
    )
