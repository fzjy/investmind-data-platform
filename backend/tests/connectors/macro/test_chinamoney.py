from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderSchemaError
from app.connectors.macro.transport import JsonTransportResponse
from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE,
    MacroSeriesCatalog,
    MacroSeriesDefinition,
    MacroSourceDefinition,
)


FIXTURE = Path(__file__).parent / "fixtures" / "chinamoney-response.json"


class FakeJsonTransport:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def get_json(self, url: str) -> JsonTransportResponse:
        self.calls.append(url)
        if isinstance(self._payload, Exception):
            raise self._payload
        return JsonTransportResponse(payload=self._payload, request_count=1)


def _payload() -> dict[str, object]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(fixture["payload"], dict)
    return deepcopy(fixture["payload"])


def _series_and_source() -> tuple[MacroSeriesDefinition, MacroSourceDefinition]:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.fx.usdcny")
    source = MacroSourceDefinition.model_validate(
        {
            "provider": "chinamoney",
            "priority": 1,
            "series": "USD/CNY",
            "field": "price",
            "sourceUrl": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
            "termsUrl": "https://www.chinamoney.com.cn/chinese/mtsjfu/",
            "accessMode": "public_page",
            "displayAllowed": True,
            "responseFields": ["lastDate", "vrtCode", "vrtName", "vrtEName", "price"],
        }
    )
    return series, source


def _window(start: date = date(2026, 9, 1), end: date = date(2026, 9, 30)) -> MacroFetchWindow:
    return MacroFetchWindow(start=start, end=end, as_of=datetime(2026, 9, 21, tzinfo=UTC))


def test_chinamoney_parses_page_linked_public_usdcny_static_json() -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    transport = FakeJsonTransport(_payload())
    result = ChinaMoneyMacroProvider(transport).fetch(series, source, _window())

    assert ChinaMoneyMacroProvider.provider_id == "chinamoney"
    assert result.status == "success"
    assert result.observations[0].provider_series_id == "USD/CNY"
    assert result.observations[0].observed_at == date(2026, 9, 18)
    assert result.observations[0].released_at == datetime(2026, 9, 18, 1, 15, tzinfo=UTC)
    assert result.observations[0].value == Decimal("6.7521")
    assert transport.calls == [str(source.source_url)]


@pytest.mark.parametrize(
    ("start", "end", "expected_status"),
    [
        (date(2026, 9, 18), date(2026, 9, 18), "success"),
        (date(2026, 9, 1), date(2026, 9, 17), "no_change"),
    ],
)
def test_chinamoney_handles_partial_and_out_of_window_static_history(
    start: date,
    end: date,
    expected_status: str,
) -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    result = ChinaMoneyMacroProvider(FakeJsonTransport(_payload())).fetch(
        series, source, _window(start, end)
    )

    assert result.status == expected_status


def test_chinamoney_accepts_date_without_release_time_without_inventing_time() -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    payload = _payload()
    data = payload["data"]
    assert isinstance(data, dict)
    data["lastDate"] = "2026-09-18"

    result = ChinaMoneyMacroProvider(FakeJsonTransport(payload)).fetch(series, source, _window())

    assert result.status == "success"
    assert result.observations[0].released_at is None


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("header", "CHINAMONEY_HEADER_INVALID"),
        ("identifier", "CHINAMONEY_IDENTIFIER_INVALID"),
        ("schema", "CHINAMONEY_SCHEMA_INVALID"),
    ],
)
def test_chinamoney_rejects_changed_header_identifier_and_schema(
    mutation: str,
    error_code: str,
) -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    payload = _payload()
    if mutation == "header":
        head = payload["head"]
        assert isinstance(head, dict)
        head["provider"] = "UNKNOWN"
    elif mutation == "identifier":
        records = payload["records"]
        assert isinstance(records, list) and isinstance(records[0], dict)
        records[0]["vrtCode"] = "999"
    else:
        payload["records"] = {"not": "a list"}

    result = ChinaMoneyMacroProvider(FakeJsonTransport(payload)).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None and result.error.code == error_code


def test_chinamoney_rejects_html_error_returned_for_json() -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    result = ChinaMoneyMacroProvider(
        FakeJsonTransport(ProviderSchemaError("HTTP_INVALID_JSON", request_count=1))
    ).fetch(series, source, _window())

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "HTTP_INVALID_JSON"
    assert result.request_count == 1


def test_chinamoney_rejects_unapproved_host_and_source_contract_before_request() -> None:
    from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider

    series, source = _series_and_source()
    transport = FakeJsonTransport(_payload())
    wrong_host = source.model_copy(update={"source_url": "https://example.com/ccpr.json"})
    wrong_identifier = source.model_copy(update={"series": "EUR/CNY"})
    wrong_series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(
        "cn.rate.govbond_10y"
    )

    host_result = ChinaMoneyMacroProvider(transport).fetch(series, wrong_host, _window())
    identifier_result = ChinaMoneyMacroProvider(transport).fetch(
        series, wrong_identifier, _window()
    )
    series_result = ChinaMoneyMacroProvider(transport).fetch(wrong_series, source, _window())

    assert host_result.error is not None
    assert host_result.error.code == "CHINAMONEY_HOST_NOT_ALLOWED"
    assert identifier_result.error is not None
    assert identifier_result.error.code == "CHINAMONEY_IDENTIFIER_INVALID"
    assert series_result.error is not None
    assert series_result.error.code == "CHINAMONEY_IDENTIFIER_INVALID"
    assert transport.calls == []
