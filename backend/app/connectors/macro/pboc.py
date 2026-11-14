from __future__ import annotations

from datetime import date
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
from app.connectors.macro.pboc_html import PbocTable, PbocTableParser
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


_ALLOWED_HOSTS = frozenset({"www.pbc.gov.cn"})
_ALLOWED_PATH = re.compile(
    r"^/diaochatongjisi/(?:attachDir|fileDir/resource/cms)/\d{4}/\d{2}/\d+\.htm$"
)
_EXPECTED_CANONICAL_SERIES = "cn.credit.tsf_stock_yoy"
_EXPECTED_SERIES = "AFRE(stock)"
_EXPECTED_FIELD = "Growth Rate(%)"
_EXPECTED_RESPONSE_FIELDS = (
    "项目 Items",
    "存量",
    "增速（%）",
    "Stock",
    "Growth Rate(%)",
)
_PERIOD_PATTERN = re.compile(r"^(?P<year>\d{4})\.(?P<month>[1-9]|1[0-2])$")
_PUNCTUATION = str.maketrans({"（": "(", "）": ")", "％": "%"})


class PbocTextTransport(Protocol):
    def get_text(self, url: str) -> TextTransportResponse: ...


class PbocMacroProvider:
    provider_id = "pboc"

    def __init__(self, transport: PbocTextTransport) -> None:
        self._transport = transport

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        contract_error = _validate_source(series, source)
        if contract_error is not None:
            return _failed_result(source, contract_error, request_count=0)
        try:
            response = self._transport.get_text(str(source.source_url))
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=exc.request_count)
        try:
            parsed = _parse_table(source, response.text)
        except ProviderError as exc:
            return _failed_result(source, exc, request_count=response.request_count)

        observations = tuple(
            item for item in parsed if window.start <= item.observed_at <= window.end
        )
        return ProviderFetchResult(
            provider=self.provider_id,
            status="success" if observations else "no_change",
            observations=observations,
            raw_payload={
                "sourceUrl": str(source.source_url),
                "periods": [item.observed_at.isoformat() for item in parsed],
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
        return ProviderSchemaError("PBOC_HOST_NOT_ALLOWED")
    if (
        _ALLOWED_PATH.fullmatch(parsed_url.path) is None
        or parsed_url.query
        or parsed_url.fragment
    ):
        return ProviderSchemaError("PBOC_PATH_NOT_ALLOWED")
    if (
        series.id != _EXPECTED_CANONICAL_SERIES
        or source.provider != "pboc"
        or source.series != _EXPECTED_SERIES
        or source.field != _EXPECTED_FIELD
        or source.response_fields != _EXPECTED_RESPONSE_FIELDS
        or source.access_mode != "public_page"
    ):
        return ProviderSchemaError("PBOC_IDENTIFIER_INVALID")
    return None


def _parse_table(
    source: MacroSourceDefinition,
    body: str,
) -> tuple[ProviderObservation, ...]:
    parser = PbocTableParser()
    parser.feed(body)
    parser.close()
    matching: list[tuple[ProviderObservation, ...]] = []
    for table in parser.tables:
        result = _parse_matching_table(source, table)
        if result is not None:
            matching.append(result)
    if len(matching) != 1:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    return matching[0]


def _parse_matching_table(
    source: MacroSourceDefinition,
    table: PbocTable,
) -> tuple[ProviderObservation, ...] | None:
    rows = table.rows
    data_indices = [
        index for index, row in enumerate(rows)
        if row and _cell_key(row[0]) == "社会融资规模存量AFRE(stock)"
    ]
    if not data_indices:
        return None
    if len(data_indices) != 1:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    chinese_indices = [
        index
        for index, row in enumerate(rows)
        if row and _cell_key(row[0]) == "项目Items" and _has_repeated_pairs(row, "存量", "增速（%）")
    ]
    if not chinese_indices:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    if len(chinese_indices) != 1:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    chinese_index = chinese_indices[0]
    if chinese_index == 0 or chinese_index + 1 >= len(rows):
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    if not (
        rows[chinese_index + 1]
        and _cell_key(rows[chinese_index + 1][0]) in {"", "项目Items"}
        and _has_repeated_pairs(rows[chinese_index + 1], "Stock", "Growth Rate(%)")
    ):
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")

    period_row = rows[chinese_index - 1]
    if data_indices[0] <= chinese_index + 1:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    data_index = data_indices[0]
    data_row = rows[data_index]
    if (
        len(period_row) != len(data_row)
        or not period_row
        or _cell_key(period_row[0]) not in {"", "项目Items"}
    ):
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    if len(rows[chinese_index]) != len(data_row) or len(rows[chinese_index + 1]) != len(data_row):
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")

    observations: list[ProviderObservation] = []
    previous_period: date | None = None
    blank_started = False
    for index in range(1, len(data_row), 2):
        if index + 1 >= len(data_row) or _cell_key(period_row[index]) != _cell_key(period_row[index + 1]):
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        match = _PERIOD_PATTERN.fullmatch(_cell_key(period_row[index]))
        if match is None:
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        observed_at = _parse_period(match)
        if previous_period is not None and (
            observed_at.year != previous_period.year or observed_at <= previous_period
        ):
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        previous_period = observed_at
        balance, growth = data_row[index].strip(), data_row[index + 1].strip()
        if not balance and not growth:
            blank_started = True
            continue
        if blank_started or not balance or not growth:
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        if index in table.merged_cells[data_index] or index + 1 in table.merged_cells[data_index]:
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        _finite_decimal(balance)
        value = _finite_decimal(growth)
        observations.append(
            ProviderObservation(
                provider="pboc",
                provider_series_id=_EXPECTED_SERIES,
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=value,
                source_url=str(source.source_url),
            )
        )
    if not observations:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    return tuple(observations)


def _parse_period(match: re.Match[str]) -> date:
    try:
        return date(int(match.group("year")), int(match.group("month")), 1)
    except ValueError as exc:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID") from exc


def _has_repeated_pairs(row: list[str], first: str, second: str) -> bool:
    return 3 <= len(row) <= 25 and (len(row) - 1) % 2 == 0 and all(
        [_cell_key(value) for value in row[index : index + 2]] == [_cell_key(first), _cell_key(second)]
        for index in range(1, len(row), 2)
    )


def _cell_key(value: str) -> str:
    return "".join(value.translate(_PUNCTUATION).split())


def _finite_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID") from exc
    if not parsed.is_finite():
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    return parsed


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="pboc",
        status="failed",
        observations=(),
        raw_payload={
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
        },
        request_count=request_count,
        error=error.sanitized_copy(request_count=request_count),
    )
