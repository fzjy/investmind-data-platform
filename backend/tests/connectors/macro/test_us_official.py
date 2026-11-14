from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderSchemaError
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.transport import JsonTransportResponse, TextTransportResponse
from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE,
    MacroSeriesCatalog,
    MacroSeriesDefinition,
    MacroSourceDefinition,
)


FIXTURES = Path(__file__).parent / "fixtures"
WINDOW = MacroFetchWindow(
    start=date(2025, 1, 1),
    end=date(2025, 3, 31),
    as_of=datetime(2025, 4, 1, tzinfo=UTC),
)


class FakeBlsTransport:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self._payload = payload
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse:
        self.calls.append((url, payload))
        if isinstance(self._payload, Exception):
            raise self._payload
        return JsonTransportResponse(payload=self._payload, request_count=1)


class FakeJsonTransport:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self._payload = payload
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse:
        self.calls.append((url, params))
        if isinstance(self._payload, Exception):
            raise self._payload
        return JsonTransportResponse(payload=self._payload, request_count=1)


class FakeBinaryTransport:
    def __init__(self, content: bytes | Exception) -> None:
        self._content = content
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> SimpleNamespace:
        self.calls.append(url)
        if isinstance(self._content, Exception):
            raise self._content
        return SimpleNamespace(content=self._content, request_count=1)


class FakeTextTransport:
    def __init__(self, text: str | Exception) -> None:
        self._text = text
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> TextTransportResponse:
        self.calls.append((url, params))
        if isinstance(self._text, Exception):
            raise self._text
        return TextTransportResponse(text=self._text, request_count=1)


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _fixture_payload(name: str) -> dict[str, object]:
    payload = _fixture(name)["payload"]
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


def _catalog_source(canonical_id: str, provider_id: str) -> MacroSourceDefinition:
    return next(
        source
        for source in _catalog().require(canonical_id).sources
        if source.provider == provider_id
    )


def _bea_source() -> MacroSourceDefinition:
    fixture = _fixture("bea-response.json")
    return MacroSourceDefinition.model_validate(
        {
            "provider": "bea",
            "priority": 2,
            "series": "1",
            "table": "T10101-Q",
            "field": "Gross domestic product",
            "transform": "identity",
            "sourceUrl": fixture["sourceUrl"],
            "termsUrl": "https://www.bea.gov/help/guidelines-for-citing-bea",
            "accessMode": "public_download",
            "displayAllowed": True,
            "responseFields": ["Line", "Gross domestic product", "YYYYQn"],
        }
    )


def _bea_zip() -> bytes:
    archive_files = _fixture("bea-response.json")["archiveFiles"]
    assert isinstance(archive_files, dict)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for filename, content in archive_files.items():
            assert isinstance(filename, str)
            assert isinstance(content, str)
            archive.writestr(filename, content)
    return buffer.getvalue()


def _bea_table_text() -> str:
    archive_files = _fixture("bea-response.json")["archiveFiles"]
    assert isinstance(archive_files, dict)
    table_text = archive_files["T10101-Q.csv"]
    assert isinstance(table_text, str)
    return table_text


def _bea_zip_with_table(table_text: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("T10101-Q.csv", table_text)
    return buffer.getvalue()


@pytest.mark.parametrize("line,replacement,code", [
    (2, "unexpected metadata", "BEA_NIPA_METADATA_INVALID"),
    (3, "Wrong,,2026Q1", "BEA_NIPA_HEADER_INVALID"),
])
def test_bea_refuses_metadata_and_header_contract_changes(line, replacement, code) -> None:
    from app.connectors.macro.bea import BeaMacroProvider
    lines = _bea_table_text().splitlines()
    lines[line] = replacement
    series = _catalog().require("us.gdp.real_qoq_annualized")
    fetched = BeaMacroProvider(FakeBinaryTransport(_bea_zip_with_table("\n".join(lines)))).fetch(series, _bea_source(), WINDOW)
    assert fetched.status == "failed"
    assert fetched.observations == ()
    assert fetched.error.code == code


def _treasury_body() -> str:
    body = _fixture("us-treasury-response.json")["body"]
    assert isinstance(body, str)
    return body


def _registry(
    *,
    bls: FakeBlsTransport,
    bea: FakeBinaryTransport,
    nyfed: FakeJsonTransport,
    treasury: FakeTextTransport,
) -> MacroProviderRegistry:
    from app.connectors.macro.bea import BeaMacroProvider
    from app.connectors.macro.bls import BlsMacroProvider
    from app.connectors.macro.nyfed import NyFedMacroProvider
    from app.connectors.macro.us_treasury import UsTreasuryMacroProvider

    return MacroProviderRegistry(
        (
            BlsMacroProvider(bls),
            BeaMacroProvider(bea),
            NyFedMacroProvider(nyfed),
            UsTreasuryMacroProvider(treasury),
        )
    )


@pytest.mark.parametrize(
    ("provider_id", "canonical_id"),
    [
        ("bls", "us.cpi.yoy"),
        ("bls", "us.labor.nonfarm_payroll_change"),
        ("bea", "us.gdp.real_qoq_annualized"),
        ("nyfed", "us.rate.effective_fed_funds"),
        ("us_treasury", "us.rate.treasury_10y"),
    ],
)
def test_direct_us_provider_maps_public_contract(
    provider_id: str,
    canonical_id: str,
) -> None:
    bls_transport = FakeBlsTransport(_fixture_payload("bls-response.json"))
    bea_transport = FakeBinaryTransport(_bea_zip())
    nyfed_transport = FakeJsonTransport(_fixture_payload("nyfed-response.json"))
    treasury_transport = FakeTextTransport(_treasury_body())
    registry = _registry(
        bls=bls_transport,
        bea=bea_transport,
        nyfed=nyfed_transport,
        treasury=treasury_transport,
    )
    source = (
        _bea_source()
        if provider_id == "bea"
        else _catalog_source(canonical_id, provider_id)
    )

    result = registry.require(provider_id).fetch(
        _catalog().require(canonical_id),
        source,
        WINDOW,
    )

    assert result.status == "success"
    assert result.observations
    assert all(item.provider == provider_id for item in result.observations)


def test_bls_splits_pc1_warmup_into_published_ten_year_windows() -> None:
    from app.connectors.macro.bls import BlsMacroProvider

    transport = FakeBlsTransport(_fixture_payload("bls-response.json"))
    series = _catalog().require("us.cpi.yoy")
    source = _catalog_source(series.id, "bls")
    window = MacroFetchWindow(
        start=date(2016, 1, 1),
        end=date(2025, 12, 31),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )

    result = BlsMacroProvider(transport).fetch(series, source, window)

    assert result.status == "success"
    assert result.request_count == 2
    assert [call[1] for call in transport.calls] == [
        {
            "seriesid": ["CUSR0000SA0"],
            "startyear": "2015",
            "endyear": "2024",
        },
        {
            "seriesid": ["CUSR0000SA0"],
            "startyear": "2025",
            "endyear": "2025",
        },
    ]
    assert all("registrationkey" not in payload for _, payload in transport.calls)


def test_bls_uses_decimal_transforms_without_second_scaling() -> None:
    from app.connectors.macro.bls import BlsMacroProvider

    payload = _fixture_payload("bls-response.json")
    provider = BlsMacroProvider(FakeBlsTransport(payload))
    cpi = _catalog().require("us.cpi.yoy")
    payroll = _catalog().require("us.labor.nonfarm_payroll_change")

    cpi_result = provider.fetch(cpi, _catalog_source(cpi.id, "bls"), WINDOW)
    payroll_result = provider.fetch(
        payroll,
        _catalog_source(payroll.id, "bls"),
        WINDOW,
    )

    cpi_values = {item.observed_at: item.value for item in cpi_result.observations}
    payroll_values = {item.observed_at: item.value for item in payroll_result.observations}
    assert cpi_values[date(2025, 1, 1)].quantize(Decimal("0.0001")) == Decimal("2.9910")
    assert payroll_values[date(2025, 1, 1)] == Decimal("125")
    assert payroll_values[date(2025, 2, 1)] == Decimal("151")


def test_bls_monthly_change_requests_prior_year_for_january_warmup() -> None:
    from app.connectors.macro.bls import BlsMacroProvider

    transport = FakeBlsTransport(_fixture_payload("bls-response.json"))
    series = _catalog().require("us.labor.nonfarm_payroll_change")

    result = BlsMacroProvider(transport).fetch(
        series,
        _catalog_source(series.id, "bls"),
        WINDOW,
    )

    assert result.observations[0].observed_at == date(2025, 1, 1)
    assert transport.calls[0][1]["startyear"] == "2024"


def test_bls_v1_refuses_precomputed_fields_before_any_request() -> None:
    from app.connectors.macro.bls import BlsMacroProvider

    payload: dict[str, object] = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CUSR0000SA0",
                    "data": [
                        {
                            "year": "2025",
                            "period": "M01",
                            "calculations": {"pct_changes": {"12": "3.0"}},
                        }
                    ],
                }
            ]
        },
    }
    series = _catalog().require("us.cpi.yoy")
    source = _catalog_source(series.id, "bls").model_copy(
        update={"field": "calculations.pct_changes.12", "transform": "identity"}
    )

    transport = FakeBlsTransport(payload)
    result = BlsMacroProvider(transport).fetch(series, source, WINDOW)

    assert result.status == "failed" and result.observations == ()
    assert transport.calls == []


def test_bls_v1_official_results_list_and_local_pc1_without_registration() -> None:
    from app.connectors.macro.bls import BlsMacroProvider
    series = _catalog().require("us.cpi.yoy")
    source = _catalog_source(series.id, "bls").model_copy(update={
        "api": "publicAPI/v1/timeseries/data", "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/",
    })
    payload = {"status": "REQUEST_SUCCEEDED", "Results": [{"series": [{"seriesID": "CUSR0000SA0", "data": [
        {"year": "2024", "period": "M01", "value": "100"},
        {"year": "2025", "period": "M01", "value": "103"},
    ]}]}]}
    transport = FakeBlsTransport(payload)
    fetched = BlsMacroProvider(transport).fetch(series, source, WINDOW)
    assert fetched.status == "success" and fetched.observations[0].value == Decimal("3.00")
    assert transport.calls == [("https://api.bls.gov/publicAPI/v1/timeseries/data/", {
        "seriesid": ["CUSR0000SA0"], "startyear": "2024", "endyear": "2025",
    })]
    assert fetched.observations[0].released_at is None and fetched.observations[0].revision is None


@pytest.mark.parametrize("results", [[], [None], [{"series": "invalid"}],
    [{"series": [{"seriesID": "CUSR0000SA0", "data": []}, {"seriesID": "CUSR0000SA0", "data": []}]}]])
def test_bls_results_refuses_unknown_or_ambiguous_wrapper_shapes(results) -> None:
    from app.connectors.macro.bls import BlsMacroProvider
    series = _catalog().require("us.cpi.yoy")
    source = _catalog_source(series.id, "bls").model_copy(update={
        "api": "publicAPI/v1/timeseries/data", "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/",
    })
    fetched = BlsMacroProvider(FakeBlsTransport({"status": "REQUEST_SUCCEEDED", "Results": results})).fetch(series, source, WINDOW)
    assert fetched.status == "failed" and fetched.observations == ()


def test_bea_parses_exact_public_nipa_table_and_line() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    transport = FakeBinaryTransport(_bea_zip())
    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(transport).fetch(series, _bea_source(), WINDOW)

    assert result.status == "success"
    assert result.request_count == 1
    assert len(result.observations) == 1
    assert result.observations[0].provider_series_id == "T10101-Q:1"
    assert result.observations[0].observed_at == date(2025, 1, 1)
    assert result.observations[0].value == Decimal("-0.3")
    assert transport.calls == [str(_bea_source().source_url)]


def test_bea_fails_closed_when_exact_nipa_line_is_missing() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    series = _catalog().require("us.gdp.real_qoq_annualized")
    source = _bea_source().model_copy(update={"series": "999"})

    result = BeaMacroProvider(FakeBinaryTransport(_bea_zip())).fetch(
        series,
        source,
        WINDOW,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "BEA_NIPA_LINE_MISSING"


@pytest.mark.parametrize(
    ("table_text", "expected_code"),
    [
        (
            _bea_table_text().replace(
                "Table 1.1.1. Percent Change From Preceding Period in Real Gross Domestic Product",
                "Table 1.1.2. Contributions to Percent Change in Real Gross Domestic Product",
            ),
            "BEA_NIPA_TABLE_INVALID",
        ),
        (
            _bea_table_text().replace(
                "1,Gross domestic product,2.4,-0.3",
                "1,Gross domestic income,2.4,-0.3",
            ),
            "BEA_NIPA_LINE_INVALID",
        ),
        (
            _bea_table_text().replace(
                "1,Gross domestic product,2.4,-0.3",
                "1, Gross domestic product,2.4,-0.3",
            ),
            "BEA_NIPA_LINE_INVALID",
        ),
        (
            _bea_table_text().replace(
                "1,Gross domestic product,2.4,-0.3",
                " 1,Gross domestic product,2.4,-0.3",
            ),
            "BEA_NIPA_LINE_INVALID",
        ),
    ],
)
def test_bea_fails_closed_on_wrong_table_title_or_line_description(
    table_text: str,
    expected_code: str,
) -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(FakeBinaryTransport(_bea_zip_with_table(table_text))).fetch(
        series,
        _bea_source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None
    assert result.error.code == expected_code


def test_bea_fails_closed_on_wrong_unit_metadata() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    table_text = _bea_table_text().replace("[Percent]", "[Billions of dollars]")
    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(FakeBinaryTransport(_bea_zip_with_table(table_text))).fetch(
        series,
        _bea_source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None
    assert result.error.code == "BEA_NIPA_UNIT_INVALID"


def test_bea_fails_closed_on_unknown_period_column_without_partial_success() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    table_text = _bea_table_text().replace(
        "Line,,2024Q4,2025Q1\r\n1,Gross domestic product,2.4,-0.3\r\n",
        "Line,,2024Q4,2025Q1,FUTURE\r\n1,Gross domestic product,2.4,-0.3,9.9\r\n",
    ).replace(
        "2,Personal consumption expenditures,4.0,1.8\r\n",
        "2,Personal consumption expenditures,4.0,1.8,8.8\r\n",
    )
    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(FakeBinaryTransport(_bea_zip_with_table(table_text))).fetch(
        series,
        _bea_source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None
    assert result.error.code == "BEA_NIPA_PERIOD_INVALID"


def test_bea_fails_closed_when_table_has_no_period_columns() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    table_text = (
        "Table 1.1.1. Percent Change From Preceding Period in Real Gross Domestic Product\r\n"
        "[Percent]\r\n"
        "\r\n"
        "Line,\r\n"
        "1,Gross domestic product\r\n"
    )
    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(FakeBinaryTransport(_bea_zip_with_table(table_text))).fetch(
        series,
        _bea_source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None
    assert result.error.code == "BEA_NIPA_PERIODS_MISSING"


def test_bea_fails_closed_when_exact_table_is_nested_in_archive() -> None:
    from app.connectors.macro.bea import BeaMacroProvider

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "nested/T10101-Q.csv",
            "Line,,2025Q1\r\n1,Gross domestic product,-0.3\r\n",
        )
    series = _catalog().require("us.gdp.real_qoq_annualized")

    result = BeaMacroProvider(FakeBinaryTransport(buffer.getvalue())).fetch(
        series,
        _bea_source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "BEA_NIPA_TABLE_MISSING"


def test_nyfed_parses_date_rate_and_revision_without_scaling() -> None:
    from app.connectors.macro.nyfed import NyFedMacroProvider

    transport = FakeJsonTransport(_fixture_payload("nyfed-response.json"))
    series = _catalog().require("us.rate.effective_fed_funds")
    source = _catalog_source(series.id, "nyfed")

    result = NyFedMacroProvider(transport).fetch(series, source, WINDOW)

    assert [item.value for item in result.observations] == [Decimal("4.33"), Decimal("4.33")]
    assert [item.revision for item in result.observations] == [None, None]
    assert transport.calls == [
        (
            str(source.source_url),
            {
                "startDate": "2025-01-01",
                "endDate": "2025-03-31",
                "type": "rate",
            },
        )
    ]


def test_nyfed_preserves_revision_indicator_only_in_raw_not_as_identity() -> None:
    from app.connectors.macro.nyfed import NyFedMacroProvider

    payload: dict[str, object] = {
        "refRates": [
            {
                "effectiveDate": "2025-02-03",
                "type": "EFFR",
                "percent": "4.33",
                "revisionIndicator": "Y",
            }
        ]
    }
    series = _catalog().require("us.rate.effective_fed_funds")

    result = NyFedMacroProvider(FakeJsonTransport(payload)).fetch(
        series,
        _catalog_source(series.id, "nyfed"),
        WINDOW,
    )

    assert result.observations[0].revision is None
    assert result.raw_payload["payload"]["refRates"][0]["revisionIndicator"] == "Y"


def test_treasury_parses_daily_ten_year_rate_without_scaling() -> None:
    from app.connectors.macro.us_treasury import UsTreasuryMacroProvider

    transport = FakeTextTransport(_treasury_body())
    series = _catalog().require("us.rate.treasury_10y")
    source = _catalog_source(series.id, "us_treasury")

    result = UsTreasuryMacroProvider(transport).fetch(series, source, WINDOW)

    assert [item.observed_at for item in result.observations] == [
        date(2025, 2, 3),
        date(2025, 2, 4),
    ]
    assert [item.value for item in result.observations] == [Decimal("4.54"), Decimal("4.52")]
    assert all(item.revision is None for item in result.observations)
    assert transport.calls == [
        (
            str(source.source_url),
            {
                "data": "daily_treasury_yield_curve",
                "field_tdr_date_value": "2025",
            },
        )
    ]


def test_treasury_counts_one_request_per_requested_year() -> None:
    from app.connectors.macro.us_treasury import UsTreasuryMacroProvider

    transport = FakeTextTransport(_treasury_body())
    series = _catalog().require("us.rate.treasury_10y")
    window = MacroFetchWindow(
        start=date(2024, 12, 31),
        end=date(2025, 1, 1),
        as_of=datetime(2025, 1, 2, tzinfo=UTC),
    )

    result = UsTreasuryMacroProvider(transport).fetch(
        series,
        _catalog_source(series.id, "us_treasury"),
        window,
    )

    assert result.request_count == 2
    assert [params for _, params in transport.calls] == [
        {
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value": "2024",
        },
        {
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value": "2025",
        },
    ]


def test_treasury_rejects_date_with_trailing_schema_garbage() -> None:
    from app.connectors.macro.us_treasury import UsTreasuryMacroProvider

    body = _treasury_body().replace(
        "2025-02-03T00:00:00",
        "2025-02-03garbage",
        1,
    )
    series = _catalog().require("us.rate.treasury_10y")

    result = UsTreasuryMacroProvider(FakeTextTransport(body)).fetch(
        series,
        _catalog_source(series.id, "us_treasury"),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "US_TREASURY_DATE_INVALID"


@pytest.mark.parametrize(
    ("provider_id", "canonical_id", "source", "expected_code"),
    [
        (
            "bls",
            "us.cpi.yoy",
            lambda: _catalog_source("us.cpi.yoy", "bls").model_copy(
                update={"source_url": "http://api.bls.gov/publicAPI/v1/timeseries/data/"}
            ),
            "BLS_URL_NOT_ALLOWED",
        ),
        (
            "bea",
            "us.gdp.real_qoq_annualized",
            lambda: _bea_source().model_copy(
                update={"source_url": "https://apps.bea.gov/national/Release/TXT/other.zip"}
            ),
            "BEA_URL_NOT_ALLOWED",
        ),
        (
            "nyfed",
            "us.rate.effective_fed_funds",
            lambda: _catalog_source("us.rate.effective_fed_funds", "nyfed").model_copy(
                update={"source_url": "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?extra=1"}
            ),
            "NYFED_URL_NOT_ALLOWED",
        ),
        (
            "us_treasury",
            "us.rate.treasury_10y",
            lambda: _catalog_source("us.rate.treasury_10y", "us_treasury").model_copy(
                update={"source_url": "https://user:pass@home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"}
            ),
            "US_TREASURY_URL_NOT_ALLOWED",
        ),
        (
            "bls",
            "us.cpi.yoy",
            lambda: _catalog_source("us.cpi.yoy", "bls").model_copy(
                update={"source_url": "https://api.bls.gov:bad/publicAPI/v1/timeseries/data/"}
            ),
            "BLS_URL_NOT_ALLOWED",
        ),
        (
            "bea",
            "us.gdp.real_qoq_annualized",
            lambda: _bea_source().model_copy(
                update={"source_url": "https://apps.bea.gov:bad/national/Release/TXT/Section1All_csv.zip"}
            ),
            "BEA_URL_NOT_ALLOWED",
        ),
        (
            "nyfed",
            "us.rate.effective_fed_funds",
            lambda: _catalog_source("us.rate.effective_fed_funds", "nyfed").model_copy(
                update={"source_url": "https://markets.newyorkfed.org:bad/api/rates/unsecured/effr/search.json"}
            ),
            "NYFED_URL_NOT_ALLOWED",
        ),
        (
            "us_treasury",
            "us.rate.treasury_10y",
            lambda: _catalog_source("us.rate.treasury_10y", "us_treasury").model_copy(
                update={"source_url": "https://home.treasury.gov:bad/resource-center/data-chart-center/interest-rates/pages/xml"}
            ),
            "US_TREASURY_URL_NOT_ALLOWED",
        ),
    ],
)
def test_direct_us_providers_reject_unapproved_urls_before_request(
    provider_id: str,
    canonical_id: str,
    source: Callable[[], MacroSourceDefinition],
    expected_code: str,
) -> None:
    bls_transport = FakeBlsTransport(_fixture_payload("bls-response.json"))
    bea_transport = FakeBinaryTransport(_bea_zip())
    nyfed_transport = FakeJsonTransport(_fixture_payload("nyfed-response.json"))
    treasury_transport = FakeTextTransport(_treasury_body())
    registry = _registry(
        bls=bls_transport,
        bea=bea_transport,
        nyfed=nyfed_transport,
        treasury=treasury_transport,
    )
    result = registry.require(provider_id).fetch(
        _catalog().require(canonical_id),
        source(),
        WINDOW,
    )

    assert result.status == "failed"
    assert result.error is not None and result.error.code == expected_code
    assert result.request_count == 0
    assert bls_transport.calls == []
    assert bea_transport.calls == []
    assert nyfed_transport.calls == []
    assert treasury_transport.calls == []


def test_bea_parser_is_not_enabled_in_catalog_without_verified_download_contract() -> None:
    assert all(
        source.provider != "bea"
        for series in _catalog().series
        for source in series.sources
    )


@pytest.mark.parametrize(
    "provider_id",
    ("bls", "bea", "nyfed", "us_treasury"),
)
def test_direct_us_sources_add_no_secret_configuration(provider_id: str) -> None:
    if provider_id == "bea":
        source = _bea_source()
    else:
        canonical_id = {
            "bls": "us.cpi.yoy",
            "nyfed": "us.rate.effective_fed_funds",
            "us_treasury": "us.rate.treasury_10y",
        }[provider_id]
        source = _catalog_source(canonical_id, provider_id)

    serialized = json.dumps(source.model_dump(mode="json", by_alias=True)).casefold()
    assert "apikey" not in serialized
    assert "registrationkey" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized


def test_direct_us_provider_keeps_transport_attempt_count_on_failure() -> None:
    from app.connectors.macro.nyfed import NyFedMacroProvider

    series = _catalog().require("us.rate.effective_fed_funds")
    source = _catalog_source(series.id, "nyfed")
    error = ProviderSchemaError("HTTP_INVALID_JSON", request_count=2)

    result = NyFedMacroProvider(FakeJsonTransport(error)).fetch(series, source, WINDOW)

    assert result.status == "failed"
    assert result.request_count == 2
    assert result.error is not None and result.error.request_count == 2
