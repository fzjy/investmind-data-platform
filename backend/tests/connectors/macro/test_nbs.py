from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderSchemaError
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE,
    MacroSeriesCatalog,
    MacroSeriesDefinition,
    MacroSourceDefinition,
)


FIXTURE = Path(__file__).parent / "fixtures" / "nbs-response.json"


class FakeTextTransport:
    def __init__(self, body: str | Exception) -> None:
        self._body = body
        self.calls: list[str] = []

    def get_text(self, url: str) -> TextTransportResponse:
        self.calls.append(url)
        if isinstance(self._body, Exception):
            raise self._body
        return TextTransportResponse(text=self._body, request_count=1)


def _body() -> str:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload["body"], str)
    return payload["body"]


def _series_and_source() -> tuple[MacroSeriesDefinition, MacroSourceDefinition]:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.yoy")
    source = MacroSourceDefinition.model_validate(
        {
            "provider": "nbs",
            "priority": 1,
            "series": "居民消费价格",
            "field": "同比涨跌幅（%）",
            "sourceUrl": "https://www.stats.gov.cn/sj/zxfb/202509/t20250910_1961112.html",
            "termsUrl": "https://www.stats.gov.cn/",
            "accessMode": "public_page",
            "displayAllowed": True,
            "responseFields": ["ArticleTitle", "PubDate", "居民消费价格", "同比涨跌幅（%）"],
        }
    )
    return series, source


def _window(start: date = date(2025, 8, 1), end: date = date(2025, 8, 31)) -> MacroFetchWindow:
    return MacroFetchWindow(start=start, end=end, as_of=datetime(2025, 9, 11, tzinfo=UTC))


def test_nbs_parses_recorded_public_cpi_release() -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    transport = FakeTextTransport(_body())

    result = NbsMacroProvider(transport).fetch(series, source, _window())

    assert NbsMacroProvider.provider_id == "nbs"
    assert result.status == "success"
    assert result.request_count == 1
    assert result.observations[0].provider_series_id == "居民消费价格"
    assert result.observations[0].observed_at == date(2025, 8, 1)
    assert result.observations[0].released_at == datetime(2025, 9, 10, 1, 30, tzinfo=UTC)
    assert result.observations[0].value == Decimal("-0.4")
    assert transport.calls == [str(source.source_url)]


@pytest.mark.parametrize(
    ("start", "end", "expected_status"),
    [
        (date(2025, 8, 1), date(2025, 8, 1), "success"),
        (date(2025, 7, 1), date(2025, 7, 31), "no_change"),
    ],
)
def test_nbs_handles_partial_and_out_of_window_history(
    start: date,
    end: date,
    expected_status: str,
) -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    result = NbsMacroProvider(FakeTextTransport(_body())).fetch(
        series, source, _window(start, end)
    )

    assert result.status == expected_status
    assert len(result.observations) == (1 if expected_status == "success" else 0)


def test_nbs_accepts_missing_release_date_without_inventing_one() -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    body = _body().replace(
        '<meta name="PubDate" content="2025/09/10 09:30">',
        "",
    )

    result = NbsMacroProvider(FakeTextTransport(body)).fetch(series, source, _window())

    assert result.status == "success"
    assert result.observations[0].released_at is None


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        ("<html><title>系统错误</title><body>访问异常</body></html>", "NBS_SCHEMA_INVALID"),
        (
            _body().replace("居民消费价格同比下降0.4%", "居民消费价格同比下降0.5%", 1),
            "NBS_VALUE_MISMATCH",
        ),
    ],
)
def test_nbs_rejects_html_errors_and_changed_release_schema(
    body: str,
    error_code: str,
) -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    result = NbsMacroProvider(FakeTextTransport(body)).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == error_code


@pytest.mark.parametrize(
    "body",
    [
        _body().replace(
            'content="2025年8月份居民消费价格同比下降0.4% 环比持平"',
            'content="2025年13月份居民消费价格同比下降0.4% 环比持平"',
        ),
        _body().replace(
            "2025年8月份，全国居民消费价格同比下降0.4%",
            "2025年13月份，全国居民消费价格同比下降0.4%",
        ),
        _body().replace("2025年8月份", "2025年13月份"),
    ],
    ids=("invalid-title-month", "invalid-body-month", "matching-invalid-months"),
)
def test_nbs_returns_schema_failure_for_invalid_release_month(body: str) -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    result = NbsMacroProvider(FakeTextTransport(body)).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "NBS_SCHEMA_INVALID"


def test_nbs_rejects_unapproved_host_and_identifier_before_request() -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    transport = FakeTextTransport(_body())
    wrong_host = source.model_copy(update={"source_url": "https://example.com/release.html"})
    wrong_identifier = source.model_copy(update={"field": "环比涨跌幅（%）"})
    wrong_series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.ppi.yoy")

    host_result = NbsMacroProvider(transport).fetch(series, wrong_host, _window())
    identifier_result = NbsMacroProvider(transport).fetch(series, wrong_identifier, _window())
    series_result = NbsMacroProvider(transport).fetch(wrong_series, source, _window())

    assert host_result.error is not None and host_result.error.code == "NBS_HOST_NOT_ALLOWED"
    assert identifier_result.error is not None
    assert identifier_result.error.code == "NBS_IDENTIFIER_INVALID"
    assert series_result.error is not None
    assert series_result.error.code == "NBS_IDENTIFIER_INVALID"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("source_url", "error_code"),
    [
        (
            "https://www.stats.gov.cn/xxgk/sjfb/t20250910_1961112.html",
            "NBS_PATH_NOT_ALLOWED",
        ),
        (
            "https://www.stats.gov.cn/sj/zxfb/202509/t20250910_1961112.htm",
            "NBS_PATH_NOT_ALLOWED",
        ),
        (
            "https://reader:secret@www.stats.gov.cn/sj/zxfb/202509/t20250910_1961112.html",
            "NBS_HOST_NOT_ALLOWED",
        ),
        (
            "https://www.stats.gov.cn/sj/zxfb/202509/t20250910_1961112.html?download=1",
            "NBS_PATH_NOT_ALLOWED",
        ),
    ],
    ids=("wrong-path", "wrong-suffix", "credentials", "query"),
)
def test_nbs_rejects_unproved_release_urls_before_request(
    source_url: str,
    error_code: str,
) -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    transport = FakeTextTransport(_body())
    unproved_source = source.model_copy(update={"source_url": source_url})

    result = NbsMacroProvider(transport).fetch(series, unproved_source, _window())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == error_code
    assert result.request_count == 0
    assert transport.calls == []


def test_nbs_returns_stable_failed_result_for_transport_error() -> None:
    from app.connectors.macro.nbs import NbsMacroProvider

    series, source = _series_and_source()
    result = NbsMacroProvider(
        FakeTextTransport(ProviderSchemaError("HTTP_CLIENT_ERROR", request_count=1))
    ).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "HTTP_CLIENT_ERROR"
    assert result.request_count == 1
