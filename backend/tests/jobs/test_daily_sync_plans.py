from __future__ import annotations

from datetime import date

import pytest

from app.connectors.tushare.strategies import Dataset, TushareMode
from app.stocks.jobs.daily_sync_plans import DailySyncPlanBuilder


def test_daily_plan_builder_keeps_market_global_and_merges_targets() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2), lookback_days=7)

    assert [plan.params for plan in builder.critical_market(TushareMode.STANDARD)] == [
        {"trade_date": "20260902"},
        {"trade_date": "20260902"},
        {"trade_date": "20260902"},
    ]
    assert builder.window_start == date(2026, 8, 27)
    assert builder.merge_symbols(("000001.SZ",), ("600519.SH", "000001.SZ")) == (
        "000001.SZ",
        "600519.SH",
    )


def test_daily_plan_builder_creates_calendar_and_bootstrap_plans() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2))

    calendar = builder.calendar()
    bootstrap = builder.bootstrap()

    assert [(plan.dataset, plan.params) for plan in calendar] == [
        (
            Dataset.TRADE_CALENDAR,
            {"exchange": "SSE", "start_date": "20260902", "end_date": "20260902"},
        )
    ]
    assert [plan.dataset for plan in bootstrap] == [Dataset.SECURITY, Dataset.CLASSIFICATION]


def test_daily_plan_builder_discovers_each_date_before_global_corporate_windows() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2), lookback_days=3)

    plans = builder.discovery(TushareMode.STANDARD)

    assert [plan.dataset for plan in plans] == [
        Dataset.DISCLOSURE_DATE,
        Dataset.DISCLOSURE_DATE,
        Dataset.DISCLOSURE_DATE,
        Dataset.REPURCHASE,
        Dataset.SHARE_UNLOCK,
        Dataset.SHAREHOLDER_COUNT,
    ]
    assert [plan.params for plan in plans[:3]] == [
        {"actual_date": "20260831"},
        {"actual_date": "20260901"},
        {"actual_date": "20260902"},
    ]
    assert [plan.params for plan in plans[3:]] == [
        {"start_date": "20260831", "end_date": "20260902"},
        {"start_date": "20260831", "end_date": "20260902"},
        {"start_date": "20260831", "end_date": "20260902"},
    ]


def test_daily_plan_builder_standard_financial_plans_use_sorted_unique_symbols() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2))

    plans = builder.financial(
        TushareMode.STANDARD,
        symbols=("600519.SH", "000001.SZ", "600519.SH"),
        periods=(date(2026, 6, 30),),
    )

    assert len(plans) == 18
    assert [plan.dataset for plan in plans[::2]] == [
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.DIVIDEND,
        Dataset.BUSINESS_SEGMENT,
        Dataset.TOP_HOLDER,
    ]
    assert all(
        [plan.params["ts_code"] for plan in plans[index : index + 2]]
        == ["000001.SZ", "600519.SH"]
        for index in range(0, len(plans), 2)
    )


def test_daily_plan_builder_vip_financial_plans_use_periods_except_symbol_apis() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2))

    plans = builder.financial(
        TushareMode.VIP,
        symbols=("600519.SH", "000001.SZ", "600519.SH"),
        periods=(date(2026, 6, 30), date(2026, 3, 31), date(2026, 6, 30)),
    )

    assert len(plans) == 18
    assert [plan.dataset for plan in plans] == [
        Dataset.INCOME,
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.DIVIDEND,
        Dataset.DIVIDEND,
        Dataset.BUSINESS_SEGMENT,
        Dataset.BUSINESS_SEGMENT,
        Dataset.TOP_HOLDER,
        Dataset.TOP_HOLDER,
    ]
    assert [plan.params for plan in plans[:2]] == [
        {"period": "20260331"},
        {"period": "20260630"},
    ]
    assert [plan.params for plan in plans[12:14]] == [
        {"ts_code": "000001.SZ"},
        {"ts_code": "600519.SH"},
    ]
    assert [plan.params for plan in plans[16:]] == [
        {"ts_code": "000001.SZ"},
        {"ts_code": "600519.SH"},
    ]


def test_daily_plan_builder_merges_periods_in_sorted_unique_order() -> None:
    builder = DailySyncPlanBuilder(date(2026, 9, 2))

    assert builder.merge_periods(
        (date(2026, 6, 30), date(2026, 3, 31)),
        (date(2026, 9, 30), date(2026, 6, 30)),
    ) == (date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30))


@pytest.mark.parametrize("lookback_days", (0, 32))
def test_daily_plan_builder_rejects_invalid_lookback(lookback_days: int) -> None:
    with pytest.raises(ValueError, match="lookback_days must be between 1 and 31"):
        DailySyncPlanBuilder(date(2026, 9, 2), lookback_days=lookback_days)
