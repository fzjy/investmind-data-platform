from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderAuthError, ProviderPermissionError
from app.connectors.macro.contracts import ProviderTransientError
from app.connectors.macro.transport import JsonTransportResponse, MacroHttpTransport
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


class FakeTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[dict[str, str]] = []

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonTransportResponse:
        del headers
        self.calls.append(dict(params or {}))
        if url.endswith("vintagedates"):
            return JsonTransportResponse(payload={"vintage_dates": ["2025-02-12"]}, request_count=1)
        return JsonTransportResponse(payload=self._payload, request_count=1)


class FailingTransport:
    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonTransportResponse:
        del url, params, headers
        raise ProviderTransientError("HTTP_SERVER_ERROR")


def _window() -> MacroFetchWindow:
    return MacroFetchWindow(
        start=date(2025, 1, 1),
        end=date(2025, 2, 28),
        as_of=datetime(2025, 2, 15, tzinfo=UTC),
    )


def _catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


@pytest.mark.parametrize("series_id", ["us.cpi.yoy", "us.gdp.real_qoq_annualized"])
def test_fred_reads_available_vintages_and_paginates_realtime_periods(series_id: str) -> None:
    from app.connectors.macro.fred import FredMacroProvider
    requests: list[httpx.Request] = []
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params.get("offset", "0"))
        if request.url.path.endswith("vintagedates"):
            return httpx.Response(200, json={"count": 2, "offset": offset, "vintage_dates": [["2025-02-01"], ["2025-02-12"]][offset]})
        rows = [
            {"date": "2025-01-01", "value": "103.0" if series_id == "us.cpi.yoy" else "3.0", "realtime_start": "2025-02-01", "realtime_end": "2025-02-11"},
            {"date": "2025-01-01", "value": "103.1" if series_id == "us.cpi.yoy" else "3.1", "realtime_start": "2025-02-12", "realtime_end": "9999-12-31"},
        ]
        if series_id == "us.cpi.yoy":
            rows.insert(0, {"date": "2024-01-01", "value": "100", "realtime_start": "2025-02-01", "realtime_end": "9999-12-31"})
        return httpx.Response(200, json={"count": len(rows), "offset": offset, "observations": rows[offset:offset + 1]})
    series = _catalog().require(series_id)
    result = FredMacroProvider("test-key", transport=MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)), max_attempts=1, backoff_seconds=(0,))).fetch(series, series.sources[0], _window())
    assert result.status == "success"
    assert [row.revision for row in result.observations] == ["2025-02-01", "2025-02-12"]
    assert result.request_count == (5 if series_id == "us.cpi.yoy" else 4)
    assert requests[-1].url.params["realtime_start"] == "2025-02-01"
    assert requests[-1].url.params["realtime_end"] == "2025-02-15"


def test_fred_daily_does_not_expand_vintages_or_claim_query_day_as_release() -> None:
    from app.connectors.macro.fred import FredMacroProvider
    series = _catalog().require("us.rate.treasury_10y")
    transport = FakeTransport({"observations": [{"date": "2025-01-01", "value": "4.1", "realtime_start": "2025-02-15", "realtime_end": "2025-02-15"}]})
    result = FredMacroProvider("test-key", transport=transport).fetch(series, series.sources[0], _window())
    assert result.observations[0].released_at is None
    assert result.observations[0].revision is None
    assert result.request_count == 1


def test_fred_maps_vintage_and_pc1_to_canonical_observations() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    source = series.sources[0]
    fake_transport = FakeTransport(
        {
            "observations": [
                {"realtime_start": "2025-02-12", "realtime_end": "9999-12-31", "date": "2024-01-01", "value": "100"},
                {
                    "realtime_start": "2025-02-12",
                    "realtime_end": "9999-12-31",
                    "date": "2025-01-01",
                    "value": "103.0",
                }
            ]
        }
    )
    provider = FredMacroProvider(api_key="a" * 32, transport=fake_transport)

    result = provider.fetch(series, source, _window())

    assert result.status == "success"
    assert result.observations[0].provider_series_id == "CPIAUCSL"
    assert result.observations[0].observed_at == date(2025, 1, 1)
    assert result.observations[0].released_at == datetime(2025, 2, 12, tzinfo=UTC)
    assert result.observations[0].revision == "2025-02-12"
    assert result.observations[0].value == Decimal("3.0")
    assert fake_transport.calls[1:] == [
        {
            "series_id": "CPIAUCSL",
            "api_key": "a" * 32,
            "file_type": "json",
            "observation_start": "2024-01-01",
            "observation_end": "2025-02-15",
            "units": "lin",
            "realtime_start": "2025-02-12",
            "realtime_end": "2025-02-15",
            "offset": "0", "output_type": "1", "limit": "100000", "sort_order": "asc",
        }
    ]


def test_fred_uses_lin_for_identity_transform() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.labor.unemployment_rate")
    fake_transport = FakeTransport({"observations": []})

    FredMacroProvider(api_key="a" * 32, transport=fake_transport).fetch(
        series,
        series.sources[0],
        _window(),
    )

    assert fake_transport.calls[1]["units"] == "lin"


def test_fred_computes_monthly_change_from_warmed_up_levels() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.labor.nonfarm_payroll_change")
    fake_transport = FakeTransport({"observations": [
        {"date": "2024-12-01", "value": "200", "realtime_start": "2025-02-12", "realtime_end": "9999-12-31"},
        {"date": "2025-01-01", "value": "203.1", "realtime_start": "2025-02-12", "realtime_end": "9999-12-31"},
    ]})

    result = FredMacroProvider(api_key="a" * 32, transport=fake_transport).fetch(
        series,
        series.sources[0],
        _window(),
    )

    assert fake_transport.calls[1]["units"] == "lin"
    assert fake_transport.calls[1]["observation_start"] == "2024-12-01"
    assert result.observations[0].value == Decimal("3.1")


def test_fred_omits_missing_values_and_sanitizes_payload() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    source = series.sources[0]
    fake_transport = FakeTransport(
        {
            "api_key": "payload-secret",
            "observations": [
                {"realtime_start": "2025-02-12", "realtime_end": "9999-12-31", "date": "2024-02-01", "value": "100"},
                {
                    "realtime_start": "2025-02-12",
                    "realtime_end": "9999-12-31",
                    "date": "2025-01-01",
                    "value": ".",
                },
                {
                    "realtime_start": "2025-02-12",
                    "realtime_end": "9999-12-31",
                    "date": "2025-02-01",
                    "value": "103.1",
                },
            ],
        }
    )

    result = FredMacroProvider(api_key="a" * 32, transport=fake_transport).fetch(
        series,
        source,
        _window(),
    )

    assert [observation.observed_at for observation in result.observations] == [date(2025, 2, 1)]
    assert result.observations[0].value == Decimal("3.1")
    assert "payload-secret" not in repr(result.raw_payload)


def test_fred_redacts_nested_query_and_header_secrets_but_keeps_economic_text() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    api_key = "fred-api-key-in-payload"
    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    source = series.sources[0]
    fake_transport = FakeTransport(
        {
            "notice": f"Authorization: Bearer {api_key}",
            "links": {
                "self": f"https://api.example.test/data?api_key={api_key}&units=pc1",
                "description": "CPI increased by 3.1 percent",
            },
            "observations": [],
        }
    )

    result = FredMacroProvider(api_key=api_key, transport=fake_transport).fetch(
        series,
        source,
        _window(),
    )

    assert api_key not in repr(result.raw_payload)
    assert result.raw_payload["notice"] == "Authorization: Bearer ***"
    links = result.raw_payload["links"]
    assert links == {
        "self": "https://api.example.test/data?api_key=***&units=pc1",
        "description": "CPI increased by 3.1 percent",
    }


def test_fred_redacts_structured_secret_and_client_secret_values_only() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    source = series.sources[0]
    fake_transport = FakeTransport(
        {
            "links": {
                "client": "https://api.example.test/data?client_secret=client-secret-value&units=pc1",
                "camel": "https://api.example.test/data?clientSecret=camel-secret-value&units=pc1",
                "upper": "https://api.example.test/data?SECRET=plain-secret-value&units=pc1",
                "nonSensitive": "https://api.example.test/data?note=secret&series_id=CPIAUCSL",
            },
            "headers": [
                "Client-Secret: client-secret-value",
                "SECRET: plain-secret-value",
            ],
            "description": "The report calls secrecy a concern, not a credential.",
            "observations": [],
        }
    )

    result = FredMacroProvider(api_key="configured-api-key", transport=fake_transport).fetch(
        series,
        source,
        _window(),
    )

    assert "client-secret-value" not in repr(result.raw_payload)
    assert "camel-secret-value" not in repr(result.raw_payload)
    assert "plain-secret-value" not in repr(result.raw_payload)
    assert result.raw_payload["links"] == {
        "client": "https://api.example.test/data?client_secret=***&units=pc1",
        "camel": "https://api.example.test/data?clientSecret=***&units=pc1",
        "upper": "https://api.example.test/data?SECRET=***&units=pc1",
        "nonSensitive": "https://api.example.test/data?note=secret&series_id=CPIAUCSL",
    }
    assert result.raw_payload["headers"] == [
        "Client-Secret: ***",
        "SECRET: ***",
    ]
    assert result.raw_payload["description"] == (
        "The report calls secrecy a concern, not a credential."
    )


def test_fred_returns_unavailable_without_api_key() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    fake_transport = FakeTransport({"observations": []})

    result = FredMacroProvider(api_key=None, transport=fake_transport).fetch(
        series,
        series.sources[0],
        _window(),
    )

    assert result.status == "unavailable"
    assert isinstance(result.error, ProviderAuthError)
    assert fake_transport.calls == []


def test_fred_returns_failed_result_when_transport_fails() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")

    result = FredMacroProvider(api_key="a" * 32, transport=FailingTransport()).fetch(
        series,
        series.sources[0],
        _window(),
    )

    assert result.status == "failed"
    assert isinstance(result.error, ProviderTransientError)
    assert result.raw_payload == {
        "sourceUrl": str(series.sources[0].source_url),
        "termsUrl": str(series.sources[0].terms_url),
    }


def test_fred_exhausted_transport_error_is_detached_and_keeps_attempt_count() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    api_key = "fred-api-key-for-provider-result"
    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network unavailable", request=request)

    transport = MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )
    result = FredMacroProvider(api_key=api_key, transport=transport).fetch(
        series,
        series.sources[0],
        _window(),
    )

    assert result.status == "failed"
    assert isinstance(result.error, ProviderTransientError)
    assert result.request_count == 2
    assert result.error.request_count == 2
    assert result.error.__cause__ is None
    assert result.error.__context__ is None
    assert result.error.__traceback__ is None
    assert api_key not in str(result.error)


def test_fred_returns_sanitized_permission_result_for_non_displayable_copyrighted_data() -> None:
    from app.connectors.macro.fred import FredMacroProvider

    catalog = _catalog()
    series = catalog.require("us.cpi.yoy")
    source = series.sources[0].model_copy(update={"display_allowed": False})
    fake_transport = FakeTransport(
        {
            "copyrighted": True,
            "api_key": "payload-secret",
            "observations": [],
        }
    )

    result = FredMacroProvider(api_key="a" * 32, transport=fake_transport).fetch(
        series,
        source,
        _window(),
    )

    assert result.status == "unavailable"
    assert isinstance(result.error, ProviderPermissionError)
    assert result.raw_payload == {
        "sourceUrl": str(source.source_url),
        "termsUrl": str(source.terms_url),
    }
