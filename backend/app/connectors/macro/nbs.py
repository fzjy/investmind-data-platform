from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol
from urllib.parse import urlsplit

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderSchemaError,
)
from app.connectors.macro.html_contract import PublicHtmlContractParser
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_ALLOWED_HOSTS = frozenset({"www.stats.gov.cn"})
_ALLOWED_PATH = re.compile(r"^/sj/zxfb/\d{6}/t\d{8}_\d+\.html$")
_EXPECTED_CANONICAL_SERIES = "cn.cpi.yoy"
_EXPECTED_SERIES = "居民消费价格"
_EXPECTED_FIELD = "同比涨跌幅（%）"
_EXPECTED_RESPONSE_FIELDS = (
    "ArticleTitle",
    "PubDate",
    "居民消费价格",
    "同比涨跌幅（%）",
)
_MONTH_PATTERN = r"(?:[1-9]|1[0-2])"
_TITLE_PATTERN = re.compile(
    rf"^(?P<year>\d{{4}})年(?P<month>{_MONTH_PATTERN})月份居民消费价格同比"
    r"(?P<direction>上涨|下降|持平)(?:(?P<value>\d+(?:\.\d+)?)%)?"
)
_BODY_PATTERN = re.compile(
    rf"(?P<year>\d{{4}})年(?P<month>{_MONTH_PATTERN})月份，全国居民消费价格(?:总水平)?同比"
    r"(?P<direction>上涨|下降|持平)(?:(?P<value>\d+(?:\.\d+)?)%)?。"
)
_CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


class NbsTextTransport(Protocol):
    def get_text(self, url: str) -> TextTransportResponse: ...


class NbsMacroProvider:
    provider_id = "nbs"

    def __init__(self, transport: NbsTextTransport) -> None:
        self._transport = transport

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        if series.id in {"cn.cpi.core_yoy", "cn.industry.value_added_yoy"}:
            from app.connectors.macro.nbs_release_feed import fetch_release_feed

            return fetch_release_feed(self._transport, series, source, window)
        contract_error = _validate_source(series, source)
        if contract_error is not None:
            return _failed_result(source, contract_error, request_count=0)
        try:
            response = self._transport.get_text(str(source.source_url))
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=exc.request_count)
        try:
            observation, title = _parse_release(source, response.text)
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
            raw_payload={"sourceUrl": str(source.source_url), "articleTitle": title},
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
        return ProviderSchemaError("NBS_HOST_NOT_ALLOWED")
    if (
        _ALLOWED_PATH.fullmatch(parsed_url.path) is None
        or parsed_url.query
        or parsed_url.fragment
    ):
        return ProviderSchemaError("NBS_PATH_NOT_ALLOWED")
    if (
        series.id != _EXPECTED_CANONICAL_SERIES
        or source.provider != "nbs"
        or source.series != _EXPECTED_SERIES
        or source.field != _EXPECTED_FIELD
        or source.response_fields != _EXPECTED_RESPONSE_FIELDS
        or source.access_mode != "public_page"
    ):
        return ProviderSchemaError("NBS_IDENTIFIER_INVALID")
    return None


def _parse_release(
    source: MacroSourceDefinition,
    body: str,
) -> tuple[ProviderObservation, str]:
    parser = PublicHtmlContractParser()
    parser.feed(body)
    title = parser.meta.get("articletitle")
    if title is None:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    title_match = _TITLE_PATTERN.match(title)
    body_match = _BODY_PATTERN.search(parser.compact_text)
    if title_match is None or body_match is None:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")

    title_identity = _identity(title_match)
    body_identity = _identity(body_match)
    if title_identity != body_identity:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    year, month, value = title_identity
    released_at = _released_at(parser.meta.get("pubdate"))
    return (
        ProviderObservation(
            provider="nbs",
            provider_series_id=_EXPECTED_SERIES,
            observed_at=date(year, month, 1),
            released_at=released_at,
            revision=None,
            value=value,
            source_url=str(source.source_url),
        ),
        title,
    )


def _identity(match: re.Match[str]) -> tuple[int, int, Decimal]:
    year = int(match.group("year"))
    month = int(match.group("month"))
    try:
        date(year, month, 1)
    except ValueError as exc:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc

    direction = match.group("direction")
    raw_value = match.group("value")
    if direction == "持平":
        value = Decimal("0")
    elif raw_value is None:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    else:
        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
        if direction == "下降":
            value = -value
    return year, month, value


def _released_at(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    try:
        local_time = datetime.strptime(raw_value, "%Y/%m/%d %H:%M").replace(
            tzinfo=_CHINA_STANDARD_TIME
        )
    except ValueError as exc:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
    return local_time.astimezone(UTC)


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="nbs",
        status="failed",
        observations=(),
        raw_payload={
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
        },
        request_count=request_count,
        error=error.sanitized_copy(request_count=request_count),
    )
