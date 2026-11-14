from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.normalizers import (
    normalize_daily_price,
    normalize_disclosure_schedule,
    normalize_financial_row,
    normalize_security,
    parse_date,
)


def test_parse_date_accepts_tushare_date() -> None:
    assert parse_date("20260814").isoformat() == "2026-08-14"


def test_parse_date_accepts_tushare_datetime() -> None:
    assert parse_date("2025-05-12 15:09:08").isoformat() == "2025-05-12"


def test_normalize_security_maps_source_fields() -> None:
    result = normalize_security(
        {
            "ts_code": "600519.SH",
            "symbol": "600519",
            "name": "贵州茅台",
            "exchange": "SSE",
            "list_date": "20010827",
            "market": "主板",
            "list_status": "L",
        }
    )

    assert result["ts_code"] == "600519.SH"
    assert result["list_date"].isoformat() == "2001-08-27"
    assert result["board"] == "主板"


def test_normalize_daily_price_converts_numbers() -> None:
    result = normalize_daily_price(
        {"ts_code": "600519.SH", "trade_date": "20260814", "close": "1488.20", "vol": "12"}
    )

    assert result["trade_date"].isoformat() == "2026-08-14"
    assert result["close"] == Decimal("1488.20")
    assert result["vol"] == Decimal("12")


def test_normalize_financial_row_preserves_pit_fields() -> None:
    result = normalize_financial_row(
        {"ts_code": "600519.SH", "ann_date": "20260430", "end_date": "20260331", "update_flag": "1", "n_income_attr_p": "300"}
    )

    assert result["ann_date"].isoformat() == "2026-04-30"
    assert result["end_date"].isoformat() == "2026-03-31"
    assert result["update_flag"] == "1"
    assert result["data"]["n_income_attr_p"] == Decimal("300")


def test_normalize_disclosure_schedule_preserves_dates_and_payload() -> None:
    result = normalize_disclosure_schedule(
        {
            "ts_code": "600519.SH",
            "ann_date": "20260820",
            "end_date": "20260630",
            "pre_date": "20260830",
            "actual_date": "20260902",
            "modify_date": "20260831",
        }
    )

    assert result["ts_code"] == "600519.SH"
    assert result["end_date"] == date(2026, 6, 30)
    assert result["actual_date"] == date(2026, 9, 2)
    assert result["data"]["actual_date"] == "20260902"
