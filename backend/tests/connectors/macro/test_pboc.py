from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE,
    MacroSeriesCatalog,
    MacroSeriesDefinition,
    MacroSourceDefinition,
)


FIXTURE = Path(__file__).parent / "fixtures" / "pboc-table.json"


class FakeTextTransport:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[str] = []

    def get_text(self, url: str) -> TextTransportResponse:
        self.calls.append(url)
        return TextTransportResponse(text=self._body, request_count=1)


def _body() -> str:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload["body"], str)
    return payload["body"]


def _series_and_source() -> tuple[MacroSeriesDefinition, MacroSourceDefinition]:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(
        "cn.credit.tsf_stock_yoy"
    )
    source = MacroSourceDefinition.model_validate(
        {
            "provider": "pboc",
            "priority": 1,
            "series": "AFRE(stock)",
            "field": "Growth Rate(%)",
            "sourceUrl": "https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/09/2026091417325051118.htm",
            "termsUrl": "https://www.pbc.gov.cn/rmyh/109345/index.html",
            "accessMode": "public_page",
            "displayAllowed": True,
            "responseFields": ["项目 Items", "存量", "增速（%）", "Stock", "Growth Rate(%)"],
        }
    )
    return series, source


def _window(start: date = date(2026, 7, 1), end: date = date(2026, 8, 31)) -> MacroFetchWindow:
    return MacroFetchWindow(start=start, end=end, as_of=datetime(2026, 9, 21, tzinfo=UTC))


def test_pboc_parses_recorded_afre_stock_growth_table() -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    result = PbocMacroProvider(FakeTextTransport(_body())).fetch(series, source, _window())

    assert PbocMacroProvider.provider_id == "pboc"
    assert [item.observed_at for item in result.observations] == [
        date(2026, 7, 1),
        date(2026, 8, 1),
    ]
    assert [item.value for item in result.observations] == [Decimal("7.4"), Decimal("7.2")]
    assert all(item.released_at is None for item in result.observations)


@pytest.mark.parametrize(
    ("start", "end", "expected_dates"),
    [
        (date(2026, 8, 1), date(2026, 8, 31), [date(2026, 8, 1)]),
        (date(2026, 6, 1), date(2026, 6, 30), []),
    ],
)
def test_pboc_filters_partial_and_out_of_window_history(
    start: date,
    end: date,
    expected_dates: list[date],
) -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    result = PbocMacroProvider(FakeTextTransport(_body())).fetch(
        series, source, _window(start, end)
    )

    assert [item.observed_at for item in result.observations] == expected_dates
    assert result.status == ("success" if expected_dates else "no_change")


@pytest.mark.parametrize(
    "body",
    [
        "<html><title>页面不存在</title><body>404</body></html>",
        _body().replace("Growth Rate(%)", "Growth(%)"),
        _body().replace("AFRE(stock)", "AFRE"),
    ],
)
def test_pboc_rejects_html_errors_changed_headers_and_identifier(body: str) -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    result = PbocMacroProvider(FakeTextTransport(body)).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "PBOC_SCHEMA_INVALID"


def test_pboc_returns_schema_failure_for_invalid_period_month() -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    body = _body().replace("2026.8", "2026.13")

    result = PbocMacroProvider(FakeTextTransport(body)).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "PBOC_SCHEMA_INVALID"


def test_pboc_rejects_unapproved_host_and_source_contract_before_request() -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    transport = FakeTextTransport(_body())
    wrong_host = source.model_copy(update={"source_url": "https://example.com/table.htm"})
    wrong_headers = source.model_copy(update={"response_fields": ("Growth Rate(%)",)})
    wrong_series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.money.m2_yoy")

    host_result = PbocMacroProvider(transport).fetch(series, wrong_host, _window())
    header_result = PbocMacroProvider(transport).fetch(series, wrong_headers, _window())
    series_result = PbocMacroProvider(transport).fetch(wrong_series, source, _window())

    assert host_result.error is not None and host_result.error.code == "PBOC_HOST_NOT_ALLOWED"
    assert header_result.error is not None and header_result.error.code == "PBOC_IDENTIFIER_INVALID"
    assert series_result.error is not None
    assert series_result.error.code == "PBOC_IDENTIFIER_INVALID"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("source_url", "error_code"),
    [
        (
            "https://www.pbc.gov.cn/diaochatongjisi/2026/09/table.htm",
            "PBOC_PATH_NOT_ALLOWED",
        ),
        (
            "https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/09/2026091417325051118.html",
            "PBOC_PATH_NOT_ALLOWED",
        ),
        (
            "https://reader:secret@www.pbc.gov.cn/diaochatongjisi/attachDir/2026/09/2026091417325051118.htm",
            "PBOC_HOST_NOT_ALLOWED",
        ),
        (
            "https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/09/2026091417325051118.htm?download=1",
            "PBOC_PATH_NOT_ALLOWED",
        ),
    ],
    ids=("wrong-path", "wrong-suffix", "credentials", "query"),
)
def test_pboc_rejects_unproved_table_urls_before_request(
    source_url: str,
    error_code: str,
) -> None:
    from app.connectors.macro.pboc import PbocMacroProvider

    series, source = _series_and_source()
    transport = FakeTextTransport(_body())
    unproved_source = source.model_copy(update={"source_url": source_url})

    result = PbocMacroProvider(transport).fetch(series, unproved_source, _window())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == error_code
    assert result.request_count == 0
    assert transport.calls == []
