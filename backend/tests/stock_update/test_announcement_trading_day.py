from datetime import date, datetime, timezone

import pytest

from app.shared.trading_days import TradingDays, announcement_dates


def july(day):
    return date(2026, 7, day)


def history():
    return TradingDays({july(3), july(7), july(9)}, july(3), july(9),
                       {july(4): False, july(5): False, july(8): True}, {july(6)})


@pytest.mark.parametrize("utc_time, expected_day, clock", [
    ("03T01:00", 3, "09:00"), ("03T06:59", 3, "14:59"),
    ("03T07:00", 7, "15:00"), ("03T08:00", 7, "16:00"), ("04T04:00", 7, "12:00"),
    ("03T00:00", 3, None), ("02T16:00", 3, None),
])
def test_disclosure_uses_shanghai_close_calendar_and_stock_resumption(utc_time, expected_day, clock):
    stamp = datetime.fromisoformat("2026-07-" + utc_time).replace(tzinfo=timezone.utc)
    result = announcement_dates(stamp, history())
    assert (result["placementStatus"], result["date"], result["disclosureTime"]) == (
        "placed", july(expected_day).isoformat(), clock)
    if utc_time == "03T07:00":
        assert result["placementReason"] == "收盘后披露、休市、个股停牌"
    if utc_time == "02T16:00":
        assert result["disclosedOn"] == "2026-07-03"


@pytest.mark.parametrize("utc_time, expected_status", [
    ("07T07:10", "pending"), ("09T07:00", "pending"), ("02T01:00", "outside"),
])
def test_unproven_trading_day_is_not_moved_to_a_nearby_candle(utc_time, expected_status):
    stamp = datetime.fromisoformat("2026-07-" + utc_time)
    assert announcement_dates(stamp, history())["placementStatus"] == expected_status
