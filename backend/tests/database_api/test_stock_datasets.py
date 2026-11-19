import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.db.models import AdjustmentFactor, DailyBasic, DailyPrice, Security


BASE = "/api/v1/datasets"
DAY = date(2026, 11, 16)
SH, SZ, BJ = "600519.SH", "000001.SZ", "832000.BJ"


@pytest.fixture
def client(monkeypatch, pg_schema_url):
    monkeypatch.setattr(main_module, "settings", Settings(database_read_url=pg_schema_url))
    application = main_module.create_app()
    try:
        with TestClient(application) as client:
            yield client
    finally:
        application.state.read_engine.dispose()


def add_securities(session, *codes):
    session.add_all(Security(ts_code=code, symbol=code[:6], name=code) for code in codes)
    session.flush()


def page(client, dataset, **params):
    response = client.get(f"{BASE}/{dataset}", params=params)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["meta"]["dataset"] == dataset
    assert payload["meta"]["coverage_scope"] == "stored"
    return payload


def test_daily_returns_stored_unadjusted_columns(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, SH)
        session.add(DailyPrice(
            ts_code=SH, trade_date=DAY, open=11, high=12, low=9, close=10,
            pre_close=10, change=0, pct_chg=0, vol=1200, amount=None,
            data={"close": 88, "amount": 999},
        ))
        session.add(AdjustmentFactor(ts_code=SH, trade_date=DAY, adj_factor=2))

    payload = page(client, "stock-daily", symbol=SH, trade_date=DAY)
    assert payload["data"] == [{
        "symbol": SH, "trade_date": "2026-11-16", "open": 11, "high": 12,
        "low": 9, "close": 10, "pre_close": 10, "change": 0, "pct_chg": 0,
        "vol": 1200, "amount": None,
    }]
    assert payload["meta"]["next_cursor"] is None


def test_adjustment_rows_have_their_own_security_date_keys(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, BJ, SH, SZ)
        session.add(DailyPrice(ts_code=SH, trade_date=DAY, close=10))
        session.add(AdjustmentFactor(ts_code=BJ, trade_date=DAY, adj_factor=Decimal("1.123456789012")))
        session.add(AdjustmentFactor(ts_code=SZ, trade_date=DAY, adj_factor=None))
        session.add(AdjustmentFactor(ts_code=SH, trade_date=DAY - timedelta(days=1), adj_factor=2))

    payload = page(client, "stock-adjustment", trade_date=DAY)
    assert payload["data"] == [
        {"symbol": SZ, "trade_date": "2026-11-16", "adj_factor": None},
        {"symbol": BJ, "trade_date": "2026-11-16", "adj_factor": 1.123456789012},
    ]
    assert page(client, "stock-adjustment", trade_date=DAY, symbol=BJ)["data"] == [payload["data"][1]]


def test_daily_basic_preserves_typed_nulls_and_reads_legacy_json_extensions(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, SH, SZ)
        session.add(DailyBasic(
            ts_code=SH, trade_date=DAY, pe=None, pb=0, ps=2, total_mv=1480,
            circ_mv=900, turnover_rate=1, dv_ratio=2, dv_ttm=3,
            data={"pe": 100, "pb": 42, "pe_ttm": "-12.25", "ps_ttm": 1.25,
                  "total_share": "150", "float_share": 0, "free_share": None},
        ))
        session.add(DailyBasic(
            ts_code=SZ, trade_date=DAY, data={"pe_ttm": "--", "free_share": "NaN"},
        ))

    payload = page(client, "stock-daily-basic", symbol=SH, trade_date=DAY)
    assert payload["data"] == [{
        "symbol": SH, "trade_date": "2026-11-16", "pe": None, "pb": 0, "ps": 2,
        "total_mv": 1480, "circ_mv": 900, "turnover_rate": 1, "dv_ratio": 2, "dv_ttm": 3,
        "pe_ttm": -12.25, "ps_ttm": 1.25, "total_share": 150, "float_share": 0, "free_share": None,
    }]
    row, = page(client, "stock-daily-basic", symbol=SZ, trade_date=DAY)["data"]
    assert row["pe_ttm"] is None
    assert row["free_share"] is None
    assert row["total_share"] is None


def test_decimal_values_are_exact_json_numbers(client, pg_factory):
    amount = Decimal("9876543210987654.12345678")
    factor = Decimal("12345.123456789012")
    pe_ttm = Decimal("-1234.1234567890123456789")
    with pg_factory.begin() as session:
        add_securities(session, SH)
        session.add(DailyPrice(ts_code=SH, trade_date=DAY, amount=amount))
        session.add(AdjustmentFactor(ts_code=SH, trade_date=DAY, adj_factor=factor))
        session.add(DailyBasic(ts_code=SH, trade_date=DAY, data={"pe_ttm": str(pe_ttm)}))

    values = {}
    for dataset, field in (
        ("stock-daily", "amount"),
        ("stock-adjustment", "adj_factor"),
        ("stock-daily-basic", "pe_ttm"),
    ):
        response = client.get(f"{BASE}/{dataset}", params={"symbol": SH, "trade_date": DAY})
        assert response.status_code == 200, response.text
        row, = json.loads(response.content, parse_float=Decimal)["data"]
        values[field] = row[field]
    assert values == {"amount": amount, "adj_factor": factor, "pe_ttm": pe_ttm}


def test_single_symbol_range_pages_include_both_boundaries(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, SH, SZ)
        for offset in (3, -1, 2, 1, 0):
            session.add(DailyPrice(ts_code=SH, trade_date=DAY + timedelta(days=offset), close=10))
        session.add(DailyPrice(ts_code=SZ, trade_date=DAY, close=99))

    params = {"symbol": SH, "start_date": DAY, "end_date": "2026-11-18", "limit": 2}
    first = page(client, "stock-daily", **params)
    assert [(row["symbol"], row["trade_date"]) for row in first["data"]] == [
        (SH, "2026-11-16"), (SH, "2026-11-17"),
    ]
    cursor = first["meta"]["next_cursor"]
    assert cursor
    last = page(client, "stock-daily", **params, cursor=cursor)
    assert [(row["symbol"], row["trade_date"]) for row in last["data"]] == [(SH, "2026-11-18")]
    assert last["meta"]["next_cursor"] is None


def test_cross_section_cursor_pages_by_symbol_and_is_bound_to_query(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, SH, SZ, BJ)
        for code in (BJ, SH, SZ):
            session.add(DailyBasic(ts_code=code, trade_date=DAY))

    first = page(client, "stock-daily-basic", trade_date=DAY, limit=1)
    assert [row["symbol"] for row in first["data"]] == [SZ]
    cursor = first["meta"]["next_cursor"]
    assert cursor
    second = page(client, "stock-daily-basic", trade_date=DAY, limit=1, cursor=cursor)
    assert [row["symbol"] for row in second["data"]] == [SH]
    last = page(client, "stock-daily-basic", trade_date=DAY, limit=1, cursor=second["meta"]["next_cursor"])
    assert [row["symbol"] for row in last["data"]] == [BJ]
    assert last["meta"]["next_cursor"] is None

    for dataset, params in (
        ("stock-daily", {"trade_date": DAY}),
        ("stock-daily-basic", {"trade_date": DAY, "symbol": SH}),
        ("stock-daily-basic", {"trade_date": "2026-11-17"}),
    ):
        response = client.get(f"{BASE}/{dataset}", params={**params, "cursor": cursor})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_all_datasets_return_empty_stored_pages_for_days_without_rows(client):
    for dataset in ("stock-daily", "stock-adjustment", "stock-daily-basic"):
        payload = page(client, dataset, trade_date=DAY)
        assert payload["data"] == []
        assert payload["meta"]["next_cursor"] is None


def test_default_page_size_and_maximum_limit(client, pg_factory):
    with pg_factory.begin() as session:
        add_securities(session, SH)
        session.add_all(
            DailyBasic(ts_code=SH, trade_date=DAY + timedelta(days=offset))
            for offset in range(501)
        )

    params = {"symbol": SH, "start_date": DAY, "end_date": DAY + timedelta(days=500)}
    first = page(client, "stock-daily-basic", **params)
    assert len(first["data"]) == 500
    assert first["meta"]["next_cursor"]
    full = page(client, "stock-daily-basic", **params, limit=2000)
    assert len(full["data"]) == 501
    assert full["meta"]["next_cursor"] is None


def test_invalid_filters_and_cursor_use_api_error_envelope(client):
    invalid_params = (
        {}, {"symbol": SH}, {"start_date": DAY, "end_date": DAY},
        {"symbol": SH, "start_date": DAY}, {"symbol": SH, "end_date": DAY},
        {"symbol": SH, "start_date": "2026-11-17", "end_date": DAY},
        {"trade_date": DAY, "start_date": DAY}, {"trade_date": "2026-02-30"},
        {"trade_date": DAY, "symbol": "600519"}, {"trade_date": DAY, "symbol": "600519.HK"},
        {"trade_date": DAY, "limit": 0}, {"trade_date": DAY, "limit": 2001},
        {"trade_date": DAY, "cursor": "invalid"}, {"trade_date": DAY, "cursor": ""},
        {"trade_date": DAY, "unknown": "value"},
    )
    for params in invalid_params:
        response = client.get(f"{BASE}/stock-daily", params=params)
        assert response.status_code == 400, (params, response.text)
        payload = response.json()
        assert payload["error"]["code"] == "INVALID_REQUEST"
        assert payload["meta"]["requestId"]


@pytest.mark.parametrize("params", [
    [("trade_date", DAY), ("symbol", SH), ("symbol", SZ)],
    [("trade_date", DAY), ("trade_date", "2026-11-17")],
    [("trade_date", DAY), ("limit", 1), ("limit", 2)],
], ids=["symbol", "trade_date", "limit"])
def test_repeated_query_parameter_is_invalid(client, params):
    response = client.get(f"{BASE}/stock-daily", params=params)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
