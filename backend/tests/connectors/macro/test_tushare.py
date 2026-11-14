from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json

import httpx
import pytest

from app.connectors.macro.contracts import (
    MacroFetchWindow,
    ProviderAuthError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTransientError,
    UnavailableMacroProvider,
)
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import (
    TushareAuthError,
    TusharePermissionError,
    TushareRateLimitError,
    TushareSchemaError,
    TushareTransientError,
)
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


class FakeTushareClient:
    def __init__(self, response: TushareResponse | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    def call(
        self,
        api_name: str,
        params: dict[str, str],
        fields: tuple[str, ...],
    ) -> TushareResponse:
        self.calls.append((api_name, params, fields))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


def _window() -> MacroFetchWindow:
    return MacroFetchWindow(
        start=date(2025, 1, 1),
        end=date(2025, 3, 31),
        as_of=datetime(2025, 4, 1, tzinfo=UTC),
    )


def _source_for(series_id: str):
    series = _catalog().require(series_id)
    return series, next(source for source in series.sources if source.provider == "tushare")


@pytest.mark.parametrize("series_id,expected", [
    ("cn.gdp.real_yoy", {"start_q": "2025Q1", "end_q": "2025Q1"}),
    ("cn.cpi.yoy", {"start_m": "202501", "end_m": "202503"}),
    ("cn.ppi.yoy", {"start_m": "202501", "end_m": "202503"}),
    ("cn.pmi.manufacturing", {"start_m": "202501", "end_m": "202503"}),
    ("cn.money.m1_yoy", {"start_m": "202501", "end_m": "202503"}),
    ("cn.money.m2_yoy", {"start_m": "202501", "end_m": "202503"}),
    ("us.rate.treasury_10y", {"start_date": "20250101", "end_date": "20250331"}),
])
def test_registered_tushare_windows_follow_public_contract(series_id, expected) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider
    series, source = _source_for(series_id)
    client = FakeTushareClient(TushareResponse(fields=source.response_fields, items=()))
    assert TushareMacroProvider(client).fetch(series, source, _window()).status == "no_change"
    assert client.calls[0][1] == expected


@pytest.mark.parametrize("returned_code", ["USDCNY.TEST", "EURUSD.TEST"])
def test_explicit_fx_selectors_are_sent_and_return_identity_is_checked(returned_code) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider
    from app.macro.catalog import MacroSourceDefinition
    series = _catalog().require("cn.fx.usdcny")
    source = MacroSourceDefinition(provider="tushare", priority=1, api="fx_daily", field="bid_close", sourceUrl="https://tushare.pro/document/2?doc_id=179", accessMode="api", displayAllowed=True, params={"ts_code": "USDCNY.TEST", "exchange": "FXCM"}, responseFields=("trade_date", "bid_close", "ts_code", "exchange"))
    client = FakeTushareClient(TushareResponse(fields=source.response_fields, items=(("20250102", "7.1", returned_code, "FXCM"),)))
    result = TushareMacroProvider(client).fetch(series, source, _window())
    assert client.calls[0][1] == {"start_date": "20250101", "end_date": "20250331", "ts_code": "USDCNY.TEST", "exchange": "FXCM"}
    if returned_code != "USDCNY.TEST":
        assert result.status == "failed"
        assert result.observations == ()
    else:
        assert result.status == "success"
        assert "USDCNY.TEST" in result.observations[0].provider_series_id


def test_unknown_tushare_params_fail_before_network() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider
    series, source = _source_for("cn.cpi.yoy")
    source = source.model_copy(update={"params": {"unknown_selector": "unproven"}})
    client = FakeTushareClient(TushareResponse(fields=source.response_fields, items=()))
    assert TushareMacroProvider(client).fetch(series, source, _window()).status == "failed"
    assert client.calls == []


def test_tushare_maps_catalog_api_fields_and_monthly_values() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("cn.cpi.yoy")
    client = FakeTushareClient(
        TushareResponse(
            fields=("month", "nt_yoy"),
            items=(("202501", "0.5"),),
        )
    )

    result = TushareMacroProvider(client).fetch(series, source, _window())

    assert result.status == "success"
    assert result.observations[0].provider_series_id == "cn_cpi:nt_yoy"
    assert result.observations[0].observed_at == date(2025, 1, 1)
    assert result.observations[0].value == Decimal("0.5")
    assert client.calls == [
        ("cn_cpi", {"start_m": "202501", "end_m": "202503"}, ("month", "nt_yoy"))
    ]


@pytest.mark.parametrize(
    ("series_id", "row", "expected_date"),
    [
        ("cn.gdp.real_yoy", ("2025Q1", "5.4"), date(2025, 1, 1)),
        ("us.rate.treasury_10y", ("20250102", "4.5"), date(2025, 1, 2)),
    ],
)
def test_tushare_parses_configured_quarterly_and_daily_date_fields(
    series_id: str,
    row: tuple[str, str],
    expected_date: date,
) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for(series_id)
    client = FakeTushareClient(
        TushareResponse(fields=source.response_fields, items=(row,))
    )

    result = TushareMacroProvider(client).fetch(series, source, _window())

    assert result.observations[0].observed_at == expected_date


@pytest.mark.parametrize("missing_value", (None, "", "."))
def test_tushare_omits_rows_with_missing_values(missing_value: object) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("cn.cpi.yoy")
    client = FakeTushareClient(
        TushareResponse(fields=source.response_fields, items=(("202501", missing_value),))
    )

    result = TushareMacroProvider(client).fetch(series, source, _window())

    assert result.status == "no_change"
    assert result.observations == ()


@pytest.mark.parametrize(
    ("vendor_error", "provider_error"),
    [
        (TushareAuthError("token real-token"), ProviderAuthError),
        (TusharePermissionError("permission"), ProviderPermissionError),
        (TushareRateLimitError("rate limit"), ProviderRateLimitError),
        (TushareTransientError("offline"), ProviderTransientError),
        (TushareSchemaError("invalid"), ProviderSchemaError),
    ],
)
def test_tushare_maps_vendor_errors_without_exposing_token(
    vendor_error: Exception,
    provider_error: type[Exception],
) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("cn.cpi.yoy")
    result = TushareMacroProvider(FakeTushareClient(vendor_error)).fetch(
        series,
        source,
        _window(),
    )

    assert result.status == "failed"
    assert isinstance(result.error, provider_error)
    assert result.request_count == 1
    assert "real-token" not in repr(result.raw_payload)
    assert "real-token" not in str(result.error)


def test_tushare_parse_error_keeps_one_request_count() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("cn.cpi.yoy")
    result = TushareMacroProvider(
        FakeTushareClient(
            TushareResponse(fields=source.response_fields, items=(("not-a-period", "0.5"),))
        )
    ).fetch(series, source, _window())

    assert result.status == "failed"
    assert isinstance(result.error, ProviderSchemaError)
    assert result.request_count == 1


def test_tushare_raw_payload_contains_rows_but_not_token() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("cn.cpi.yoy")
    result = TushareMacroProvider(
        FakeTushareClient(
            TushareResponse(
                fields=("month", "nt_yoy", "token"),
                items=(("202501", "0.5", "real-token"),),
            )
        )
    ).fetch(series, source, _window())

    assert result.raw_payload["rows"] == [{"month": "202501", "nt_yoy": "0.5"}]
    assert "real-token" not in repr(result.raw_payload)


def test_daily_three_windows_keep_each_response_and_selector_identity() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider
    from app.macro.catalog import MacroSourceDefinition

    series = _catalog().require("cn.fx.usdcny")
    source = MacroSourceDefinition(
        provider="tushare", priority=1, api="fx_daily", field="bid_close",
        sourceUrl="https://tushare.pro/document/2?doc_id=179",
        accessMode="api", displayAllowed=True,
        params={"ts_code": "USDCNY.TEST", "exchange": "FXCM"},
        responseFields=("trade_date", "bid_close", "ts_code", "exchange"),
    )
    window = MacroFetchWindow(date(2024, 1, 1), date(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC))
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        start = body["params"]["start_date"]
        # 中片为空也必须有窗口审计；闰年的365天片在12月30日结束。
        items = [] if start == "20241231" else [[start, "7.1", "USDCNY.TEST", "FXCM"]]
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {
            "fields": list(source.response_fields), "items": items,
        }})

    with TushareClient("synthetic-token", transport=httpx.MockTransport(handler), max_retries=0) as client:
        result = TushareMacroProvider(client).fetch(series, source, window)

    expected_params = [
        {"ts_code": "USDCNY.TEST", "exchange": "FXCM", "start_date": "20240101", "end_date": "20241230"},
        {"ts_code": "USDCNY.TEST", "exchange": "FXCM", "start_date": "20241231", "end_date": "20251230"},
        {"ts_code": "USDCNY.TEST", "exchange": "FXCM", "start_date": "20251231", "end_date": "20260101"},
    ]
    assert [body["params"] for body in sent] == expected_params
    assert result.status == "success"
    assert result.request_count == 3
    assert [row.observed_at for row in result.observations] == [date(2024, 1, 1), date(2025, 12, 31)]
    requests = result.raw_payload["requests"]
    assert [part["params"] for part in requests] == expected_params
    assert [part["window"] for part in requests] == [
        {"start": "2024-01-01", "end": "2024-12-30", "asOf": "2026-01-02T00:00:00+00:00"},
        {"start": "2024-12-31", "end": "2025-12-30", "asOf": "2026-01-02T00:00:00+00:00"},
        {"start": "2025-12-31", "end": "2026-01-01", "asOf": "2026-01-02T00:00:00+00:00"},
    ]
    for part in requests:
        assert part["apiName"] == "fx_daily"
        assert part["field"] == "bid_close"
        assert part["responseFields"] == list(source.response_fields)
        assert part["sourceUrl"] == str(source.source_url)
        assert part["status"] == "success"
    assert requests[1]["rows"] == []
    assert result.raw_payload["rows"] == [*requests[0]["rows"], *requests[2]["rows"]]
    assert "synthetic-token" not in repr(result.raw_payload)


def test_daily_retry_recovery_keeps_one_audit_per_window_without_duplicate_rows() -> None:
    from app.connectors.macro.tushare import TushareMacroProvider

    series, source = _source_for("us.rate.treasury_10y")
    window = MacroFetchWindow(date(2025, 1, 1), date(2026, 1, 2), datetime(2026, 1, 3, tzinfo=UTC))
    sent: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        sent.append(params)
        if len(sent) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {
            "fields": list(source.response_fields), "items": [[params["start_date"], "4.5"]],
        }})

    with TushareClient("synthetic-token", transport=httpx.MockTransport(handler), max_retries=1, backoff_seconds=0) as client:
        result = TushareMacroProvider(client).fetch(series, source, window)

    assert sent == [
        {"start_date": "20250101", "end_date": "20251231"},
        {"start_date": "20260101", "end_date": "20260102"},
        {"start_date": "20260101", "end_date": "20260102"},
    ]
    assert result.status == "success"
    assert len(result.observations) == 2
    assert result.request_count == 2  # 既有计数契约为client.call次数，不冒称HTTP attempt数。
    assert len(result.raw_payload["requests"]) == 2
    assert result.raw_payload["rows"] == [
        {"date": "20250101", "y10": "4.5"}, {"date": "20260101", "y10": "4.5"},
    ]


@pytest.mark.parametrize("second_code,second_exchange", [
    ("EURUSD.TEST", "FXCM"), ("USDCNY.TEST", "OTHER"),
])
def test_later_window_wrong_selector_fails_without_erasing_either_response(
    second_code: str, second_exchange: str,
) -> None:
    from app.connectors.macro.tushare import TushareMacroProvider
    from app.macro.catalog import MacroSourceDefinition

    series = _catalog().require("cn.fx.usdcny")
    source = MacroSourceDefinition(
        provider="tushare", priority=1, api="fx_daily", field="bid_close",
        sourceUrl="https://tushare.pro/document/2?doc_id=179",
        accessMode="api", displayAllowed=True,
        params={"ts_code": "USDCNY.TEST", "exchange": "FXCM"},
        responseFields=("trade_date", "bid_close", "ts_code", "exchange"),
    )
    window = MacroFetchWindow(date(2025, 1, 1), date(2026, 1, 2), datetime(2026, 1, 3, tzinfo=UTC))
    sent: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        sent.append(params)
        code, exchange = ("USDCNY.TEST", "FXCM") if len(sent) == 1 else (second_code, second_exchange)
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {
            "fields": list(source.response_fields),
            "items": [[params["start_date"], "7.1", code, exchange]],
        }})

    with TushareClient("synthetic-token", transport=httpx.MockTransport(handler), max_retries=0) as client:
        result = TushareMacroProvider(client).fetch(series, source, window)

    assert result.status == "failed" and result.observations == ()
    assert result.error is not None and result.error.code == "TUSHARE_SERIES_IDENTITY_MISMATCH"
    assert result.request_count == 2
    assert sent == [
        {"ts_code": "USDCNY.TEST", "exchange": "FXCM", "start_date": "20250101", "end_date": "20251231"},
        {"ts_code": "USDCNY.TEST", "exchange": "FXCM", "start_date": "20260101", "end_date": "20260102"},
    ]
    assert result.raw_payload["rows"] == [
        {"trade_date": "20250101", "bid_close": "7.1", "ts_code": "USDCNY.TEST", "exchange": "FXCM"},
        {"trade_date": "20260101", "bid_close": "7.1", "ts_code": second_code, "exchange": second_exchange},
    ]
    parts = result.raw_payload["requests"]
    assert [part["params"] for part in parts] == sent
    assert [part["rows"] for part in parts] == [[result.raw_payload["rows"][0]], [result.raw_payload["rows"][1]]]


def test_registry_rejects_duplicates_and_returns_registered_provider() -> None:
    error = ProviderAuthError("TUSHARE_TOKEN_MISSING")
    provider = UnavailableMacroProvider("tushare", error)
    registry = MacroProviderRegistry([provider])

    assert registry.require("tushare") is provider
    with pytest.raises(ValueError, match="duplicate macro provider id: tushare"):
        MacroProviderRegistry([provider, provider])
    with pytest.raises(LookupError, match="macro provider not registered: fred"):
        registry.require("fred")
