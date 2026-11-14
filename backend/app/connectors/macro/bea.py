from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO, StringIO
import re
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderError,
    ProviderFetchResult,
    ProviderObservation,
    ProviderSchemaError,
)
from app.connectors.macro.transport import BinaryTransportResponse
from app.connectors.macro.url_policy import is_exact_public_url
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


BEA_NIPA_DOWNLOAD_URL = (
    "https://apps.bea.gov/national/Release/TXT/Section1All_csv.zip"
)
_BEA_HOST = "apps.bea.gov"
_BEA_PATH = "/national/Release/TXT/Section1All_csv.zip"
_NIPA_TABLE_TITLE = (
    "Table 1.1.1. Percent Change From Preceding Period in Real Gross Domestic Product"
)
_NIPA_UNIT = "[Percent]"
_QUARTER = re.compile(r"^(?P<year>\d{4})Q(?P<quarter>[1-4])$")
_MAX_MEMBER_BYTES = 20 * 1024 * 1024


class BeaTransport(Protocol):
    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> BinaryTransportResponse: ...


class BeaMacroProvider:
    provider_id = "bea"

    def __init__(self, transport: BeaTransport) -> None:
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
            response = self._transport.get_bytes(str(source.source_url))
        except ProviderError as exc:
            return _failed_result(source, exc)

        audit = {
            "sourceUrl": str(source.source_url),
            "termsUrl": str(source.terms_url) if source.terms_url is not None else None,
            "contentType": "application/zip",
            "contentSha256": sha256(response.content).hexdigest(),
            "table": source.table,
            "line": source.series,
        }
        try:
            observations = _read_bea_public_nipa(response.content, source, window)
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
        host=_BEA_HOST,
        path=_BEA_PATH,
    ):
        raise ProviderSchemaError("BEA_URL_NOT_ALLOWED")
    if (
        series.id != "us.gdp.real_qoq_annualized"
        or source.provider != "bea"
        or source.table != "T10101-Q"
        or source.series is None
        or source.field != "Gross domestic product"
        or source.transform != "identity"
    ):
        raise ProviderSchemaError("BEA_IDENTIFIER_INVALID")


def _read_bea_public_nipa(
    content: bytes,
    source: MacroSourceDefinition,
    window: MacroFetchWindow,
) -> tuple[ProviderObservation, ...]:
    if source.table is None or source.series is None:
        raise ProviderSchemaError("BEA_IDENTIFIER_INVALID")
    expected_member = f"{source.table}.csv"
    try:
        with ZipFile(BytesIO(content)) as archive:
            matches = [
                info
                for info in archive.infolist()
                if info.filename == expected_member
            ]
            if len(matches) != 1:
                raise ProviderSchemaError("BEA_NIPA_TABLE_MISSING")
            member = matches[0]
            if member.file_size > _MAX_MEMBER_BYTES:
                raise ProviderSchemaError("BEA_DOWNLOAD_TOO_LARGE")
            table_bytes = archive.read(member)
    except ProviderError:
        raise
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ProviderSchemaError("BEA_DOWNLOAD_INVALID") from exc
    try:
        table_text = table_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderSchemaError("BEA_DOWNLOAD_INVALID") from exc

    rows = list(csv.reader(StringIO(table_text)))
    if len(rows) < 4 or rows[0] != [_NIPA_TABLE_TITLE]:
        raise ProviderSchemaError("BEA_NIPA_TABLE_INVALID")
    if rows[1] != [_NIPA_UNIT]:
        raise ProviderSchemaError("BEA_NIPA_UNIT_INVALID")
    if rows[2] != []:
        raise ProviderSchemaError("BEA_NIPA_METADATA_INVALID")

    header = rows[3]
    if len(header) < 2 or header[:2] != ["Line", ""]:
        raise ProviderSchemaError("BEA_NIPA_HEADER_INVALID")
    raw_periods = header[2:]
    if not raw_periods:
        raise ProviderSchemaError("BEA_NIPA_PERIODS_MISSING")
    periods: list[tuple[int, int]] = []
    for period in raw_periods:
        quarter_match = _QUARTER.fullmatch(period)
        if quarter_match is None:
            raise ProviderSchemaError("BEA_NIPA_PERIOD_INVALID")
        periods.append(
            (
                int(quarter_match.group("year")),
                int(quarter_match.group("quarter")),
            )
        )

    matches = [
        row
        for row in rows[4:]
        if len(row) >= 2 and row[0].strip() == source.series
    ]
    if len(matches) != 1:
        raise ProviderSchemaError("BEA_NIPA_LINE_MISSING")
    row = matches[0]
    if (
        row[0] != source.series
        or row[1] != source.field
        or len(row) != len(header)
    ):
        raise ProviderSchemaError("BEA_NIPA_LINE_INVALID")

    observations: list[ProviderObservation] = []
    for (year, quarter), raw_value in zip(periods, row[2:], strict=True):
        observed_at = date(year, (quarter - 1) * 3 + 1, 1)
        if observed_at < window.start or observed_at > window.end:
            continue
        try:
            value = Decimal(raw_value.strip().replace(",", ""))
        except InvalidOperation as exc:
            raise ProviderSchemaError("BEA_VALUE_INVALID") from exc
        if not value.is_finite():
            raise ProviderSchemaError("BEA_VALUE_INVALID")
        observations.append(
            ProviderObservation(
                provider="bea",
                provider_series_id=f"{source.table}:{source.series}",
                observed_at=observed_at,
                released_at=None,
                revision=None,
                value=value,
                source_url=str(source.source_url),
            )
        )
    return tuple(observations)


def _failed_result(
    source: MacroSourceDefinition,
    error: ProviderError,
    *,
    request_count: int | None = None,
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    safe_count = error.request_count if request_count is None else request_count
    return ProviderFetchResult(
        provider="bea",
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
