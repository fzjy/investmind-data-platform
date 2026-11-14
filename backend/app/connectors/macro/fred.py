from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol
from zoneinfo import ZoneInfo

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderAuthError,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderPermissionError,
    ProviderSchemaError,
)
from app.connectors.macro.transport import JsonTransportResponse
from app.connectors.macro.fred_transform import level_window_start, transformed_observations
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_VINTAGES_URL = "https://api.stlouisfed.org/fred/series/vintagedates"
_SECRET_KEY_PARTS = frozenset(
    {"apikey", "token", "authorization", "password", "secret"}
)
_SECRET_KEY_PATTERN = (
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|secret|token|authorization|password)"
)
_SECRET_QUERY_VALUE = re.compile(
    rf"(?i)([?&]{_SECRET_KEY_PATTERN}=)[^&#\s]+"
)
_SECRET_HEADER_VALUE = re.compile(
    rf"(?i)(?<![?&])\b({_SECRET_KEY_PATTERN})\s*[:=]\s*((?:bearer\s+)?)[^\s,;]+"
)


class FredTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse: ...


class FredMacroProvider:
    provider_id = "fred"

    def __init__(self, api_key: str | None, *, transport: FredTransport) -> None:
        self._api_key = api_key
        self._transport = transport

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        if not self._api_key:
            return _unavailable_result(
                source,
                ProviderAuthError("FRED_API_KEY_MISSING"),
            )
        if source.series is None:
            return _failed_result(source, ProviderSchemaError("FRED_SERIES_MISSING"))

        request_count = 0
        pages: list[dict[str, object]] = []
        params = {"series_id": source.series, "api_key": self._api_key, "file_type": "json"}

        def read_pages(url: str, query: dict[str, str], key: str) -> dict[str, object]:
            nonlocal request_count
            offset = 0
            rows: list[object] = []
            while True:
                response = self._transport.get_json(url, params={**params, **query, "offset": str(offset)})
                request_count += response.request_count
                pages.append(_sanitize_payload(response.payload, api_key=self._api_key or ""))
                values = response.payload.get(key)
                if not isinstance(values, list):
                    raise ProviderSchemaError("FRED_PAGE_INVALID")
                count = response.payload.get("count", len(values))
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ProviderSchemaError("FRED_PAGE_COUNT_INVALID")
                returned_offset = response.payload.get("offset", offset)
                if returned_offset != offset or (not values and offset < count):
                    raise ProviderSchemaError("FRED_PAGE_BOUNDARY_INVALID")
                rows.extend(values)
                offset += len(values)
                if offset >= count:
                    return {**response.payload, key: rows}

        try:
            observation_start = level_window_start(series, source, window.start)
            realtime_start = window.as_of.date().isoformat()
            if series.frequency != "daily":
                vintage_payload = read_pages(FRED_VINTAGES_URL, {
                    "realtime_start": window.start.isoformat(),
                    "realtime_end": window.as_of.date().isoformat(),
                    "limit": "10000", "sort_order": "asc",
                }, "vintage_dates")
                raw_dates = vintage_payload["vintage_dates"]
                assert isinstance(raw_dates, list)
                dates = sorted({_required_date(value, "FRED_VINTAGE_INVALID") for value in raw_dates})
                if any(value < window.start or value > window.as_of.date() for value in dates):
                    raise ProviderSchemaError("FRED_VINTAGE_BOUNDARY_INVALID")
                if not dates:
                    return ProviderFetchResult(provider="fred", status="no_change", observations=(), raw_payload={"pages": pages}, request_count=request_count)
                realtime_start = dates[0].isoformat()
            response_payload = read_pages(FRED_OBSERVATIONS_URL, {
                "observation_start": observation_start.isoformat(),
                "observation_end": min(window.end, window.as_of.date()).isoformat(),
                "units": "lin",
                "realtime_start": realtime_start,
                "realtime_end": window.as_of.date().isoformat(),
                "output_type": "1", "limit": "100000", "sort_order": "asc",
            }, "observations")
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=request_count + exc.request_count, raw_payload={"pages": pages} if pages else None)
        payload = _sanitize_payload(response_payload, api_key=self._api_key)
        payload["pages"] = pages
        if _is_copyrighted(response_payload) and not source.display_allowed:
            return ProviderFetchResult(
                provider=self.provider_id,
                status="unavailable",
                observations=(),
                raw_payload={
                    "sourceUrl": str(source.source_url),
                    "termsUrl": str(source.terms_url),
                },
                request_count=request_count,
                error=ProviderPermissionError("FRED_DISPLAY_NOT_ALLOWED"),
            )

        try:
            observations = (
                _observations(source, response_payload, window, historical_vintages=series.frequency != "daily")
                if source.transform == "identity"
                else transformed_observations(series, source, response_payload, window)
            )
        except ProviderError as exc:
            return _failed_result(
                source,
                exc,
                request_count=request_count,
                raw_payload=payload,
            )
        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload=payload,
            request_count=request_count,
        )


class CalendarFredMacroProvider(FredMacroProvider):
    """生产入口共用的保守来源日历；底层解析和 job 检查时刻不变。"""

    def fetch(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
              window: MacroFetchWindow) -> ProviderFetchResult:
        # 观测期与修订可见期是不同维度：旧窗口也可有今天发布的修订。
        day = window.as_of.astimezone(ZoneInfo('America/Chicago')).date()
        end = min(window.end, day)
        cutoff = min(window.as_of.astimezone(UTC), datetime.combine(day, time.max, tzinfo=UTC))
        calendar = {'timezone': 'America/Chicago', 'end': day.isoformat(),
                    'observationEnd': end.isoformat(), 'asOf': cutoff.isoformat()}
        if window.start > end:
            # 只请求今天而源日历仍为昨日：不能发送倒置窗口或报在线成功。
            return ProviderFetchResult(provider='fred', status='no_change', observations=(),
                raw_payload={'sourceCalendar': calendar}, request_count=0)
        # URL 与归一化 cutoff 必须同时收紧，否则请求截断会误关有效期。
        result = super().fetch(series, source, replace(window, end=end, as_of=cutoff))
        return replace(result, raw_payload={**result.raw_payload, 'sourceCalendar': calendar})


def _observations(
    source: MacroSourceDefinition,
    payload: Mapping[str, object],
    window: MacroFetchWindow,
    *, historical_vintages: bool,
) -> tuple[ProviderObservation, ...]:
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        raise ProviderSchemaError("FRED_OBSERVATIONS_MISSING")
    if source.series is None:
        raise ProviderSchemaError("FRED_SERIES_MISSING")

    observations: list[ProviderObservation] = []
    for raw_observation in raw_observations:
        if not isinstance(raw_observation, Mapping):
            raise ProviderSchemaError("FRED_OBSERVATION_INVALID")
        value = raw_observation.get("value")
        if value == "." or value is None:
            continue
        if not isinstance(value, str):
            raise ProviderSchemaError("FRED_VALUE_INVALID")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ProviderSchemaError("FRED_VALUE_INVALID") from exc
        observed_at = _required_date(raw_observation.get("date"), "FRED_DATE_INVALID")
        if observed_at < window.start or observed_at > window.end:
            continue
        realtime_start = raw_observation.get("realtime_start")
        released_at = (
            datetime.combine(
                _required_date(realtime_start, "FRED_REALTIME_START_INVALID"),
                datetime.min.time(),
                tzinfo=UTC,
            )
            if historical_vintages and realtime_start is not None
            else None
        )
        realtime_end = raw_observation.get("realtime_end")
        if realtime_end is not None:
            _required_date(realtime_end, "FRED_REALTIME_END_INVALID")
        if released_at is not None and released_at > window.as_of:
            continue
        revision = released_at.date().isoformat() if released_at is not None else None
        observations.append(
            ProviderObservation(
                provider="fred",
                provider_series_id=source.series,
                observed_at=observed_at,
                released_at=released_at,
                revision=revision,
                value=decimal_value,
                source_url=str(source.source_url),
            )
        )
    return tuple(observations)


def _required_date(value: object, error_code: str) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError(error_code)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError(error_code) from exc


def _is_copyrighted(payload: Mapping[str, object]) -> bool:
    for key in ("copyrighted", "copyright"):
        value = payload.get(key)
        if value is True or (isinstance(value, str) and value.strip()):
            return True
    return False


def _sanitize_payload(
    payload: Mapping[str, object],
    *,
    api_key: str,
) -> dict[str, object]:
    return {
        key: _sanitize_value(value, api_key=api_key)
        for key, value in payload.items()
        if not _is_secret_key(key)
    }


def _sanitize_value(value: object, *, api_key: str) -> object:
    if isinstance(value, Mapping):
        return {
            key: _sanitize_value(nested, api_key=api_key)
            for key, nested in value.items()
            if isinstance(key, str) and not _is_secret_key(key)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_value(item, api_key=api_key) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, api_key=api_key)
    return value


def _sanitize_string(value: str, *, api_key: str) -> str:
    redacted = value.replace(api_key, "***")
    redacted = _SECRET_QUERY_VALUE.sub(r"\1***", redacted)
    return _SECRET_HEADER_VALUE.sub(
        lambda match: f"{match.group(1)}: {match.group(2)}***",
        redacted,
    )


def _is_secret_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _unavailable_result(
    source: MacroSourceDefinition,
    error: ProviderError,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="fred",
        status="unavailable",
        observations=(),
        raw_payload={
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url),
        },
        request_count=0,
        error=error,
    )


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int | None = None,
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    safe_error = error.sanitized_copy(request_count=request_count)
    return ProviderFetchResult(
        provider="fred",
        status="failed",
        observations=(),
        raw_payload=raw_payload
        if raw_payload is not None
        else {
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url),
        },
        request_count=safe_error.request_count,
        error=safe_error,
    )
