from __future__ import annotations

from datetime import date

import pytest

from app.connectors.tushare.strategies import (
    Dataset,
    FIELD_REGISTRY,
    SyncScope,
    TushareMode,
    build_request_plans,
)


def test_derived_input_fields_are_requested_from_tushare() -> None:
    expected = {
        Dataset.DAILY_BASIC: {
            "total_share",
            "float_share",
            "free_share",
            "pe_ttm",
            "ps_ttm",
            "limit_status",
        },
        Dataset.INCOME: {
            "total_revenue",
            "total_profit",
            "income_tax",
            "n_income",
            "fin_exp_int_exp",
        },
        Dataset.BALANCE_SHEET: {
            "money_cap",
            "inventories",
            "goodwill",
            "st_borr",
            "lt_borr",
            "st_bonds_payable",
            "non_cur_liab_due_1y",
            "bond_payable",
            "lease_liab",
            "total_share",
        },
        Dataset.CASHFLOW: {
            "c_recp_cap_contrib",
            "free_cashflow",
            "c_pay_dist_dpcp_int_exp",
            "c_cash_equ_end_period",
        },
        Dataset.FINANCIAL_INDICATOR: {
            "fcff",
            "interestdebt",
            "netdebt",
            "invest_capital",
            "tax_to_ebt",
            "debt_to_eqt",
            "ocf_to_or",
        },
    }

    for dataset, fields in expected.items():
        assert fields <= set(FIELD_REGISTRY[dataset])


def test_financial_standard_plans_by_symbol() -> None:
    plans = list(
        build_request_plans(
            Dataset.INCOME,
            TushareMode.STANDARD,
            SyncScope(symbols=("600519.SH",), start_date=date(2023, 1, 1)),
        )
    )

    assert plans[0].api_name == "income"
    assert plans[0].params["ts_code"] == "600519.SH"
    assert plans[0].cursor_key == "income:600519.SH"


def test_financial_vip_plans_by_period() -> None:
    plans = list(
        build_request_plans(
            Dataset.INCOME,
            TushareMode.VIP,
            SyncScope(periods=(date(2025, 12, 31),)),
        )
    )

    assert plans[0].api_name == "income_vip"
    assert plans[0].params["period"] == "20251231"


def test_financial_indicator_requests_only_supported_identity_fields() -> None:
    plan = build_request_plans(
        Dataset.FINANCIAL_INDICATOR,
        TushareMode.STANDARD,
        SyncScope(symbols=("600519.SH",)),
    )[0]

    assert {"ann_date", "end_date", "update_flag"} <= set(plan.fields)
    assert "report_type" not in plan.fields


def test_classification_plan_requests_official_index_classify_fields() -> None:
    plan = build_request_plans(
        Dataset.CLASSIFICATION,
        TushareMode.STANDARD,
        SyncScope(),
    )[0]

    assert plan.fields == (
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    )


def test_daily_standard_plans_split_by_symbol() -> None:
    plans = list(
        build_request_plans(
            Dataset.DAILY,
            TushareMode.STANDARD,
            SyncScope(symbols=("600519.SH", "000858.SZ"), start_date=date(2026, 8, 1)),
        )
    )

    assert [plan.params["ts_code"] for plan in plans] == ["600519.SH", "000858.SZ"]


def test_disclosure_date_plans_each_day_in_lookback_window() -> None:
    plans = build_request_plans(
        Dataset.DISCLOSURE_DATE,
        TushareMode.STANDARD,
        SyncScope(trade_dates=(date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))),
    )

    assert [plan.api_name for plan in plans] == ["disclosure_date"] * 3
    assert [plan.params for plan in plans] == [
        {"actual_date": "20260831"},
        {"actual_date": "20260901"},
        {"actual_date": "20260902"},
    ]
    assert [plan.cursor_key for plan in plans] == [
        "disclosure_date:actual:20260831",
        "disclosure_date:actual:20260901",
        "disclosure_date:actual:20260902",
    ]


@pytest.mark.parametrize(
    "dataset",
    (Dataset.REPURCHASE, Dataset.SHARE_UNLOCK, Dataset.SHAREHOLDER_COUNT),
)
def test_corporate_action_supports_one_global_date_window(dataset: Dataset) -> None:
    plans = build_request_plans(
        dataset,
        TushareMode.STANDARD,
        SyncScope(start_date=date(2026, 8, 27), end_date=date(2026, 9, 2)),
    )

    assert len(plans) == 1
    assert plans[0].params == {"start_date": "20260827", "end_date": "20260902"}
    assert plans[0].cursor_key == f"{dataset.value}:window:20260827:20260902"


def test_top_holder_does_not_support_a_global_date_window() -> None:
    plans = build_request_plans(
        Dataset.TOP_HOLDER,
        TushareMode.STANDARD,
        SyncScope(start_date=date(2026, 8, 27), end_date=date(2026, 9, 2)),
    )

    assert plans == []


@pytest.mark.parametrize(
    ("dataset", "api_name"),
    [
        (Dataset.SECURITY, "stock_basic"),
        (Dataset.TRADE_CALENDAR, "trade_cal"),
        (Dataset.DAILY, "daily"),
        (Dataset.DAILY_BASIC, "daily_basic"),
        (Dataset.ADJ_FACTOR, "adj_factor"),
        (Dataset.INCOME, "income"),
        (Dataset.BALANCE_SHEET, "balancesheet"),
        (Dataset.CASHFLOW, "cashflow"),
        (Dataset.FINANCIAL_INDICATOR, "fina_indicator"),
        (Dataset.FINANCIAL_FORECAST, "forecast"),
        (Dataset.FINANCIAL_EXPRESS, "express"),
        (Dataset.DIVIDEND, "dividend"),
        (Dataset.REPURCHASE, "repurchase"),
        (Dataset.SHARE_UNLOCK, "share_float"),
        (Dataset.SHAREHOLDER_COUNT, "stk_holdernumber"),
        (Dataset.TOP_HOLDER, "top10_holders"),
        (Dataset.BUSINESS_SEGMENT, "fina_mainbz"),
        (Dataset.CLASSIFICATION, "index_classify"),
    ],
)
def test_standard_plan_uses_registered_tushare_api_name(
    dataset: Dataset,
    api_name: str,
) -> None:
    plans = build_request_plans(
        dataset,
        TushareMode.STANDARD,
        SyncScope(
            symbols=("600519.SH",),
            trade_dates=(date(2026, 8, 17),),
            periods=(date(2025, 12, 31),),
            start_date=date(2023, 8, 17),
            end_date=date(2026, 8, 17),
        ),
    )

    assert plans
    assert plans[0].api_name == api_name


@pytest.mark.parametrize(
    ("dataset", "api_name"),
    [
        (Dataset.INCOME, "income_vip"),
        (Dataset.BALANCE_SHEET, "balancesheet_vip"),
        (Dataset.CASHFLOW, "cashflow_vip"),
        (Dataset.FINANCIAL_INDICATOR, "fina_indicator_vip"),
        (Dataset.FINANCIAL_FORECAST, "forecast_vip"),
        (Dataset.FINANCIAL_EXPRESS, "express_vip"),
        (Dataset.BUSINESS_SEGMENT, "fina_mainbz_vip"),
    ],
)
def test_vip_plan_uses_registered_period_api_name(
    dataset: Dataset,
    api_name: str,
) -> None:
    plans = build_request_plans(
        dataset,
        TushareMode.VIP,
        SyncScope(
            symbols=("600519.SH",),
            periods=(date(2025, 12, 31),),
        ),
    )

    assert plans[0].api_name == api_name
    assert plans[0].params == {"period": "20251231"}


def test_dividend_uses_symbol_api_in_vip_mode() -> None:
    plans = build_request_plans(
        Dataset.DIVIDEND,
        TushareMode.VIP,
        SyncScope(
            symbols=("600519.SH",),
            periods=(date(2025, 12, 31),),
        ),
    )

    assert plans[0].api_name == "dividend"
    assert plans[0].params["ts_code"] == "600519.SH"
