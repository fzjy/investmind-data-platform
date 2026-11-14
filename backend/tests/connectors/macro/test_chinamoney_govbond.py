from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.db.base import Base, ExternalBase
from app.db.models import IngestionBatch, MacroObservation, SourcePayload
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_sync_repository import MacroObservationRepository

from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider
from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.transport import MacroHttpTransport
from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition, MacroSourceDefinition,
)


ENDPOINT = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis"
DATES = (
    "2026-09-30", "2026-09-29", "2026-09-28", "2026-09-24", "2026-09-23",
    "2026-09-22", "2026-09-21", "2026-09-20", "2026-09-18", "2026-09-17",
    "2026-09-16", "2026-09-15", "2026-09-14", "2026-09-11", "2026-09-10",
    "2026-09-09", "2026-09-08", "2026-09-07", "2026-09-04", "2026-09-03",
    "2026-09-02", "2026-09-01",
)
YIELDS = (
    "1.6750", "1.6722", "1.6767", "1.6739", "1.6776", "1.6775", "1.6800",
    "1.6787", "1.6825", "1.6840", "1.6848", "1.6857", "1.6860", "1.6850",
    "1.6819", "1.6791", "1.6798", "1.6792", "1.6803", "1.6811", "1.6861", "1.6833",
)


def _source(**updates: object) -> MacroSourceDefinition:
    payload: dict[str, object] = {
        "provider": "chinamoney", "priority": 1, "series": "CGB-10Y",
        "api": "SddsIntrRateGovYldHis", "field": "tenRate", "transform": "identity",
        "sourceUrl": ENDPOINT, "termsUrl": "https://www.chinamoney.com.cn/chinese/mtsjfu/",
        "accessMode": "public_page", "displayAllowed": True,
        "responseFields": ["dateString", "tenRate"], "params": {"maxPages": "24"},
    }
    payload.update(updates)
    return MacroSourceDefinition.model_validate(payload)


def _window() -> MacroFetchWindow:
    return MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 10, 7, tzinfo=UTC))


def _pages() -> list[dict[str, object]]:
    rows = [
        {"dsplyTm": None, "dateString": day, "one": None, "oneRate": "1.2",
         "ten": None, "tenRate": value}
        for day, value in zip(DATES, YIELDS, strict=True)
    ]
    return [
        {
            "head": {"version": "2.0", "provider": "CWAP", "req_code": "",
                     "rep_code": "200", "rep_message": "", "ts": 1791376760864,
                     "producer": "", "tstext": "2026-10-07 20:39:20"},
            "data": {"total": 22, "endDateCN": "2026-09-30", "pageTotal": 2,
                     "pageSize": 15, "startDateCN": "2026-09-01", "pageNum": index,
                     "flagMessage": ""},
            "records": rows[index * 15 : (index + 1) * 15],
        }
        for index in range(2)
    ]


def _fetch(
    pages: list[dict[str, object]], *, source: MacroSourceDefinition | None = None,
    window: MacroFetchWindow | None = None, series: MacroSeriesDefinition | None = None,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        index = int(request.url.params["pageNum"]) - 1
        return httpx.Response(200, json=deepcopy(pages[index]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,)) as transport:
            selected = series or MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.rate.govbond_10y")
            result = ChinaMoneyMacroProvider(transport).fetch(selected, source or _source(), window or _window())
    return result, requests


def test_govbond_history_reads_both_pages_without_using_one_year_or_real_time_quotes() -> None:
    result, requests = _fetch(_pages())

    assert result.status == "success"
    assert result.request_count == 2
    assert len(result.observations) == 22
    assert len({item.observed_at for item in result.observations}) == 22
    values = {item.observed_at: item.value for item in result.observations}
    assert values[date(2026, 9, 30)] == Decimal("1.6750")
    assert values[date(2026, 9, 1)] == Decimal("1.6833")
    assert all(item.released_at is None and item.revision is None for item in result.observations)
    assert all(item.source_url == ENDPOINT for item in result.observations)
    assert requests[1].method == "POST" and requests[1].content == b"{}"
    assert parse_qs(urlsplit(str(requests[1].url)).query) == {
        "lang": ["CN"], "startDate": ["2026-09-01"], "endDate": ["2026-09-30"],
        "pageNum": ["2"], "pageSize": ["15"],
    }


def test_govbond_budget_rejects_partial_history_and_retains_first_page_evidence() -> None:
    result, requests = _fetch(_pages(), source=_source(params={"maxPages": "1"}))

    assert result.error is not None and result.error.code == "CHINAMONEY_PAGE_BUDGET_EXCEEDED"
    assert result.observations == () and result.request_count == 1 and len(requests) == 1
    assert len(result.raw_payload["pages"]) == 1


@pytest.mark.parametrize("params", [
    {}, {"maxPages": "0"}, {"maxPages": "241"}, {"maxPages": "1.5"},
    {"maxPages": "24", "curve_type": "1"}, {"maxPages": "-1"},
])
def test_govbond_rejects_invalid_or_unconsumed_parameters_before_http(params) -> None:
    result, requests = _fetch(_pages(), source=_source(params=params))
    assert result.error is not None and result.error.code == "CHINAMONEY_PARAMS_INVALID"
    assert result.request_count == 0 and requests == []


@pytest.mark.parametrize("url", [
    ENDPOINT + "?pageNum=5", ENDPOINT + "#fragment", ENDPOINT.replace("https:", "http:"),
    ENDPOINT.replace(".cn/", ".cn:444/"), ENDPOINT.replace("https://", "https://user:password@"),
    ENDPOINT.replace(".cn/", ".cn.example.test/"), ENDPOINT.replace("GovYldHis", "His"),
])
def test_govbond_refuses_unapproved_endpoint_without_request(url: str) -> None:
    result, requests = _fetch(_pages(), source=_source(sourceUrl=url))
    assert result.error is not None and result.error.code == "CHINAMONEY_HOST_NOT_ALLOWED"
    assert requests == [] and result.request_count == 0


@pytest.mark.parametrize("update", [
    {"series": "CGB-1Y"}, {"field": "oneRate"}, {"provider": "other"},
    {"api": "yc_cb"}, {"transform": "pc1"}, {"accessMode": "api"},
    {"responseFields": ["dateString", "oneRate"]},
])
def test_govbond_rejects_different_series_or_field_before_http(update) -> None:
    result, requests = _fetch(_pages(), source=_source(**update))
    assert result.error is not None and result.error.code == "CHINAMONEY_IDENTIFIER_INVALID"
    assert requests == []


@pytest.mark.parametrize("update", [{"region": "US"}, {"unit": "CNY"}, {"frequency": "monthly"}])
def test_govbond_rejects_wrong_canonical_metadata(update) -> None:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.rate.govbond_10y")
    result, requests = _fetch(_pages(), series=series.model_copy(update=update))
    assert result.error is not None and result.error.code == "CHINAMONEY_IDENTIFIER_INVALID"
    assert requests == []


@pytest.mark.parametrize("window", [
    MacroFetchWindow(date(2026, 9, 30), date(2026, 9, 1), datetime(2026, 10, 7, tzinfo=UTC)),
    MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 9, 20, tzinfo=UTC)),
    MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 10, 7)),
])
def test_govbond_refuses_invalid_or_future_query_window(window) -> None:
    result, requests = _fetch(_pages(), window=window)
    assert result.error is not None and result.error.code == "CHINAMONEY_WINDOW_INVALID"
    assert requests == []


@pytest.mark.parametrize("update", [
    {"total": 21}, {"total": True}, {"total": "22"}, {"total": -1},
    {"pageTotal": 1}, {"pageTotal": True}, {"pageNum": 2}, {"pageSize": 16},
    {"startDateCN": "2026-08-01"}, {"endDateCN": "2026-09-29"},
    {"flagMessage": "query rejected"}, {"flagMessage": None},
])
def test_govbond_rejects_changed_or_malformed_page_metadata(update) -> None:
    pages = _pages()
    pages[1]["data"].update(update)
    result, _ = _fetch(pages)
    assert result.status == "failed" and result.observations == ()
    assert result.error is not None and result.error.code == "CHINAMONEY_PAGINATION_INVALID"
    assert result.request_count == 2 and len(result.raw_payload["pages"]) == 2


def test_govbond_rejects_cross_page_total_change_even_when_each_page_is_individually_valid() -> None:
    pages = _pages()
    pages[1]["data"]["total"] = 23
    pages[1]["records"].append({
        "dsplyTm": None, "dateString": "2026-09-25", "one": None,
        "oneRate": "1.2", "ten": None, "tenRate": "1.6700",
    })
    result, _ = _fetch(pages)
    assert result.error is not None and result.error.code == "CHINAMONEY_PAGINATION_INVALID"
    assert result.status == "failed" and result.observations == () and result.request_count == 2
    assert len(result.raw_payload["pages"]) == 2


@pytest.mark.parametrize("price", [None, "", ".", "NaN", "Infinity", "-Infinity", True, "not-a-number"])
def test_govbond_rejects_missing_or_nonfinite_yield_without_partial_observations(price) -> None:
    pages = _pages()
    pages[1]["records"][0]["tenRate"] = price
    result, _ = _fetch(pages)
    assert result.error is not None and result.error.code == "CHINAMONEY_SCHEMA_INVALID"
    assert result.observations == () and result.request_count == 2


@pytest.mark.parametrize("day", ["2026-09-31", "2026-08-31", "2026-10-01", "20260909", None])
def test_govbond_rejects_invalid_or_outside_window_date(day) -> None:
    pages = _pages()
    pages[1]["records"][0]["dateString"] = day
    result, _ = _fetch(pages)
    assert result.error is not None and result.error.code == "CHINAMONEY_SCHEMA_INVALID"
    assert result.observations == ()


@pytest.mark.parametrize("change", ["same-duplicate", "conflicting-duplicate", "truncated", "missing-records", "missing-data", "header"])
def test_govbond_refuses_duplicates_truncation_and_unproved_empty_responses(change: str) -> None:
    pages = _pages()
    if change in {"same-duplicate", "conflicting-duplicate"}:
        pages[1]["records"][0] = deepcopy(pages[0]["records"][0])
        if change == "conflicting-duplicate":
            pages[1]["records"][0]["tenRate"] = "9.9"
    elif change == "truncated":
        pages[1]["records"].pop()
    elif change == "missing-records":
        pages[1].pop("records")
    elif change == "missing-data":
        pages[1].pop("data")
    else:
        pages[1]["head"]["provider"] = "OTHER"
    result, _ = _fetch(pages)
    assert result.status == "failed" and result.observations == () and result.request_count == 2
    assert len(result.raw_payload["pages"]) == 2


def test_govbond_second_page_http_failure_retains_raw_and_counts_failed_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pages()[0]) if request.url.params["pageNum"] == "1" else httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,)) as transport:
            series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.rate.govbond_10y")
            result = ChinaMoneyMacroProvider(transport).fetch(series, _source(), _window())
    assert result.error is not None and result.error.code == "HTTP_PERMISSION_DENIED"
    assert result.observations == () and result.request_count == 2
    assert len(result.raw_payload["pages"]) == 1


def test_govbond_proven_empty_window_is_no_change_not_missing_schema() -> None:
    page = _pages()[0]
    page["data"].update(total=0, pageTotal=0)
    page["records"] = []
    result, requests = _fetch([page])
    assert result.status == "no_change" and result.error is None
    assert result.request_count == 1 and len(requests) == 1 and result.observations == ()


def test_govbond_window_uses_china_business_date_at_utc_day_boundary() -> None:
    window = MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 30), datetime(2026, 9, 29, 16, tzinfo=UTC))
    result, _ = _fetch(_pages(), window=window)
    assert result.status == "success" and len(result.observations) == 22


def test_govbond_real_repository_keeps_raw_pages_capture_time_and_idempotent_values() -> None:
    result, _ = _fetch(_pages())
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.rate.govbond_10y")
    captured_at = datetime(2026, 10, 7, 13, tzinfo=UTC)
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        ExternalBase.metadata.create_all(engine)
        repository = MacroObservationRepository(factory)
        first = repository.persist(series, result, captured_at)
        repeat = repository.persist(series, result, captured_at + timedelta(days=1))
        assert (first.inserted, first.unchanged, first.rejected) == (22, 0, 0)
        assert (repeat.inserted, repeat.unchanged, repeat.rejected) == (0, 22, 0)
        with factory() as session:
            rows = session.scalars(select(MacroObservation)).all()
            assert len(rows) == 22
            assert all(row.series_id == "cn.rate.govbond_10y" for row in rows)
            assert all(row.released_at == datetime(2026, 10, 7, 13) for row in rows)
            assert all(row.revision.startswith("initial-import:") for row in rows)
            assert session.get(IngestionBatch, first.batch_id).request_count == 2
            payloads = session.scalars(select(SourcePayload)).all()
            assert len(payloads) == 2
            assert len(payloads[0].payload["data"]["pages"]) == 2
            assert payloads[0].payload["data"]["pages"][1]["response"]["records"][6]["tenRate"] == "1.6833"
    finally:
        engine.dispose()


def test_govbond_failed_history_audits_first_page_without_writing_any_core() -> None:
    result, _ = _fetch(_pages(), source=_source(params={"maxPages": "1"}))
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.rate.govbond_10y")
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        ExternalBase.metadata.create_all(engine)
        repository = MacroObservationRepository(factory)
        batch = repository.audit_request(series, _source(params={"maxPages": "1"}), _window(), result,
                                         datetime(2026, 10, 7, tzinfo=UTC), "bounded-govbond-failure")
        with factory() as session:
            assert session.scalars(select(MacroObservation)).all() == []
            assert session.get(IngestionBatch, batch).status == "failed"
            payload = session.scalar(select(SourcePayload))
            assert payload.payload["requestCount"] == 1
            assert len(payload.payload["data"]["pages"]) == 1
    finally:
        engine.dispose()
