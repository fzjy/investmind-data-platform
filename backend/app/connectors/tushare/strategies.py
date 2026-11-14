from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal


class TushareMode(StrEnum):
    STANDARD = "standard"
    VIP = "vip"


class Dataset(StrEnum):
    CLASSIFICATION_MEMBER = "classification_member"
    SECURITY = "security"
    TRADE_CALENDAR = "trade_calendar"
    DISCLOSURE_DATE = "disclosure_date"
    DAILY = "daily"
    DAILY_BASIC = "daily_basic"
    ADJ_FACTOR = "adj_factor"
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASHFLOW = "cashflow"
    FINANCIAL_INDICATOR = "financial_indicator"
    FINANCIAL_FORECAST = "financial_forecast"
    FINANCIAL_EXPRESS = "financial_express"
    DIVIDEND = "dividend"
    REPURCHASE = "repurchase"
    SHARE_UNLOCK = "share_unlock"
    SHAREHOLDER_COUNT = "shareholder_count"
    TOP_HOLDER = "top_holder"
    BUSINESS_SEGMENT = "business_segment"
    CLASSIFICATION = "classification"


BACKFILL_DATASETS = frozenset(
    {
        Dataset.SECURITY,
        Dataset.TRADE_CALENDAR,
        Dataset.DAILY,
        Dataset.DAILY_BASIC,
        Dataset.ADJ_FACTOR,
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.DIVIDEND,
        Dataset.REPURCHASE,
        Dataset.SHARE_UNLOCK,
        Dataset.SHAREHOLDER_COUNT,
        Dataset.TOP_HOLDER,
        Dataset.BUSINESS_SEGMENT,
        Dataset.CLASSIFICATION,
    }
)


@dataclass(frozen=True)
class SyncScope:
    symbols: tuple[str, ...] = ()
    trade_dates: tuple[date, ...] = ()
    periods: tuple[date, ...] = ()
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class RequestPlan:
    dataset: Dataset
    api_name: str
    params: dict[str, str]
    fields: tuple[str, ...]
    cursor_key: str
    page_size: int = 5000
    # Optional consumer requirement, never sent as a supplier API parameter.
    required_visible_since: str | None = None


# Frozen report contract used by existing stock manifests and macro evidence.
LEGACY_CASHFLOW_FIELDS = (
    "ts_code",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "update_flag",
    "n_cashflow_act",
    "c_pay_acq_const_fiolta",
    "n_cashflow_inv_act",
    "n_cash_flows_fnc_act",
    "c_recp_cap_contrib",
    "free_cashflow",
    "c_pay_dist_dpcp_int_exp",
    "c_cash_equ_end_period",
)

CASHFLOW_FIELDS = (
    *LEGACY_CASHFLOW_FIELDS,
    "c_fr_sale_sg",
    "recp_tax_rends",
    "n_depos_incr_fi",
    "n_incr_loans_cb",
    "n_inc_borr_oth_fi",
    "prem_fr_orig_contr",
    "n_incr_insured_dep",
    "n_reinsur_prem",
    "n_incr_disp_tfa",
    "ifc_cash_incr",
    "n_incr_disp_faas",
    "n_incr_loans_oth_bank",
    "n_cap_incr_repur",
    "net_cash_rece_sec",
    "c_fr_oth_operate_a",
    "c_inf_fr_operate_a",
    "c_paid_goods_s",
    "c_paid_to_for_empl",
    "c_paid_for_taxes",
    "n_incr_clt_loan_adv",
    "n_incr_dep_cbob",
    "c_pay_claims_orig_inco",
    "pay_handling_chrg",
    "pay_comm_insur_plcy",
    "net_dism_capital_add",
    "oth_cash_pay_oper_act",
    "st_cash_out_act",
    "oth_recp_ral_inv_act",
    "c_disp_withdrwl_invest",
    "c_recp_return_invest",
    "n_recp_disp_fiolta",
    "n_recp_disp_sobu",
    "stot_inflows_inv_act",
    "c_paid_invest",
    "n_disp_subs_oth_biz",
    "oth_pay_ral_inv_act",
    "n_incr_pledge_loan",
    "stot_out_inv_act",
    "incl_cash_rec_saims",
    "c_recp_borrow",
    "proc_issue_bonds",
    "oth_cash_recp_ral_fnc_act",
    "stot_cash_in_fnc_act",
    "c_prepay_amt_borr",
    "incl_dvd_profit_paid_sc_ms",
    "oth_cashpay_ral_fnc_act",
    "stot_cashout_fnc_act",
    "eff_fx_flu_cash",
    "n_incr_cash_cash_equ",
    "c_cash_equ_beg_period",
    "depr_fa_coga_dpba",
    "amort_intang_assets",
    "lt_amort_deferred_exp",
)

FIELD_REGISTRY: dict[Dataset, tuple[str, ...]] = {
    Dataset.SECURITY: ("ts_code", "symbol", "name", "exchange", "list_date", "market", "list_status"),
    Dataset.TRADE_CALENDAR: ("exchange", "cal_date", "is_open", "pretrade_date"),
    Dataset.DISCLOSURE_DATE: (
        "ts_code",
        "ann_date",
        "end_date",
        "pre_date",
        "actual_date",
        "modify_date",
    ),
    Dataset.DAILY: ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"),
    Dataset.DAILY_BASIC: (
        "ts_code",
        "trade_date",
        "turnover_rate",
        "pe",
        "pb",
        "ps",
        "total_mv",
        "circ_mv",
        "dv_ratio",
        "dv_ttm",
        "total_share",
        "float_share",
        "free_share",
        "pe_ttm",
        "ps_ttm",
        "limit_status",
    ),
    Dataset.ADJ_FACTOR: ("ts_code", "trade_date", "adj_factor"),
    Dataset.INCOME: (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "report_type",
        "comp_type",
        "update_flag",
        "n_income_attr_p",
        "basic_eps",
        "oper_cost",
        "revenue",
        "ebit",
        "total_revenue",
        "total_profit",
        "income_tax",
        "n_income",
        "fin_exp_int_exp",
    ),
    Dataset.BALANCE_SHEET: (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "report_type",
        "comp_type",
        "update_flag",
        "total_assets",
        "total_liab",
        "total_hldr_eqy_exc_min_int",
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
    ),
    Dataset.CASHFLOW: CASHFLOW_FIELDS,
    Dataset.FINANCIAL_INDICATOR: (
        "ts_code",
        "ann_date",
        "end_date",
        "update_flag",
        "roe",
        "q_eps",
        "eps",
        "bps",
        "q_roe",
        "roic",
        "grossprofit_margin",
        "netprofit_margin",
        "or_yoy",
        "netprofit_yoy",
        "fcff",
        "interestdebt",
        "netdebt",
        "invest_capital",
        "tax_to_ebt",
        "debt_to_eqt",
        "ocf_to_or",
    ),
    Dataset.DIVIDEND: ("ts_code", "ann_date", "end_date", "div_proc", "cash_div", "cash_div_tax", "record_date", "ex_date", "pay_date"),
    Dataset.FINANCIAL_FORECAST: ("ts_code", "ann_date", "end_date", "type", "p_change_min", "p_change_max", "net_profit_min", "net_profit_max"),
    Dataset.FINANCIAL_EXPRESS: ("ts_code", "ann_date", "end_date", "revenue", "n_income", "total_assets", "total_hldr_eqy_exc_min_int"),
    Dataset.REPURCHASE: ("ts_code", "ann_date", "proc", "exp_date", "vol", "amount"),
    Dataset.SHARE_UNLOCK: ("ts_code", "ann_date", "float_date", "float_share", "unlock_date"),
    Dataset.SHAREHOLDER_COUNT: ("ts_code", "ann_date", "end_date", "holder_num"),
    Dataset.TOP_HOLDER: ("ts_code", "ann_date", "end_date", "holder_name", "hold_amount", "holder_type"),
    Dataset.BUSINESS_SEGMENT: ("ts_code", "end_date", "revenue", "segment_name", "segment_type"),
    Dataset.CLASSIFICATION: (
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    ),
}


STANDARD_API_NAMES: dict[Dataset, str] = {
    Dataset.SECURITY: "stock_basic",
    Dataset.TRADE_CALENDAR: "trade_cal",
    Dataset.DISCLOSURE_DATE: "disclosure_date",
    Dataset.DAILY: "daily",
    Dataset.DAILY_BASIC: "daily_basic",
    Dataset.ADJ_FACTOR: "adj_factor",
    Dataset.INCOME: "income",
    Dataset.BALANCE_SHEET: "balancesheet",
    Dataset.CASHFLOW: "cashflow",
    Dataset.FINANCIAL_INDICATOR: "fina_indicator",
    Dataset.FINANCIAL_FORECAST: "forecast",
    Dataset.FINANCIAL_EXPRESS: "express",
    Dataset.DIVIDEND: "dividend",
    Dataset.REPURCHASE: "repurchase",
    Dataset.SHARE_UNLOCK: "share_float",
    Dataset.SHAREHOLDER_COUNT: "stk_holdernumber",
    Dataset.TOP_HOLDER: "top10_holders",
    Dataset.BUSINESS_SEGMENT: "fina_mainbz",
    Dataset.CLASSIFICATION: "index_classify",
}


VIP_API_NAMES: dict[Dataset, str] = {
    Dataset.INCOME: "income_vip",
    Dataset.BALANCE_SHEET: "balancesheet_vip",
    Dataset.CASHFLOW: "cashflow_vip",
    Dataset.FINANCIAL_INDICATOR: "fina_indicator_vip",
    Dataset.FINANCIAL_FORECAST: "forecast_vip",
    Dataset.FINANCIAL_EXPRESS: "express_vip",
    Dataset.BUSINESS_SEGMENT: "fina_mainbz_vip",
}


def _date_value(value: date | None) -> str | None:
    return value.strftime("%Y%m%d") if value else None


def _base_params(scope: SyncScope) -> dict[str, str]:
    params: dict[str, str] = {}
    if scope.start_date:
        params["start_date"] = _date_value(scope.start_date) or ""
    if scope.end_date:
        params["end_date"] = _date_value(scope.end_date) or ""
    return params


def _plan(
    dataset: Dataset,
    api_name: str,
    params: dict[str, str],
    cursor_key: str,
) -> RequestPlan:
    return RequestPlan(dataset, api_name, params, FIELD_REGISTRY[dataset], cursor_key)


def build_request_plans(
    dataset: Dataset,
    mode: TushareMode,
    scope: SyncScope,
) -> list[RequestPlan]:
    plans: list[RequestPlan] = []
    if dataset is Dataset.SECURITY:
        plans.append(_plan(dataset, STANDARD_API_NAMES[dataset], {"list_status": "L"}, "security:all"))
        return plans
    if dataset is Dataset.TRADE_CALENDAR:
        params = {"exchange": "SSE"}
        params.update(_base_params(scope))
        plans.append(_plan(dataset, STANDARD_API_NAMES[dataset], params, "trade_calendar:SSE"))
        return plans

    if dataset in {Dataset.DAILY, Dataset.DAILY_BASIC, Dataset.ADJ_FACTOR}:
        api_name = STANDARD_API_NAMES[dataset]
        for symbol in scope.symbols:
            params = {"ts_code": symbol}
            params.update(_base_params(scope))
            plans.append(_plan(dataset, api_name, params, f"{dataset.value}:{symbol}"))
        for trade_date in scope.trade_dates:
            params = {"trade_date": _date_value(trade_date) or ""}
            plans.append(_plan(dataset, api_name, params, f"{dataset.value}:{params['trade_date']}"))
        return plans

    if dataset is Dataset.CLASSIFICATION:
        plans.append(_plan(dataset, STANDARD_API_NAMES[dataset], {}, "classification:all"))
        return plans

    if dataset is Dataset.DISCLOSURE_DATE:
        return [
            _plan(
                dataset,
                STANDARD_API_NAMES[dataset],
                {"actual_date": _date_value(actual_date) or ""},
                f"disclosure_date:actual:{_date_value(actual_date)}",
            )
            for actual_date in scope.trade_dates
        ]

    financial_datasets = {
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.DIVIDEND,
        Dataset.BUSINESS_SEGMENT,
    }
    if dataset in financial_datasets:
        if mode is TushareMode.VIP and dataset in VIP_API_NAMES:
            api_name = VIP_API_NAMES[dataset]
            for period in scope.periods:
                params = {"period": _date_value(period) or ""}
                plans.append(_plan(dataset, api_name, params, f"{dataset.value}:period:{params['period']}"))
        else:
            api_name = STANDARD_API_NAMES[dataset]
            for symbol in scope.symbols:
                params = {"ts_code": symbol}
                params.update(_base_params(scope))
                plans.append(_plan(dataset, api_name, params, f"{dataset.value}:{symbol}"))
        return plans

    if dataset in {Dataset.REPURCHASE, Dataset.SHARE_UNLOCK, Dataset.SHAREHOLDER_COUNT}:
        api_name = STANDARD_API_NAMES[dataset]
        if scope.symbols:
            return [
                _plan(
                    dataset,
                    api_name,
                    {"ts_code": symbol, **_base_params(scope)},
                    f"{dataset.value}:{symbol}",
                )
                for symbol in scope.symbols
            ]
        params = _base_params(scope)
        if set(params) == {"start_date", "end_date"}:
            return [
                _plan(
                    dataset,
                    api_name,
                    params,
                    f"{dataset.value}:window:{params['start_date']}:{params['end_date']}",
                )
            ]
        return []

    if dataset is Dataset.TOP_HOLDER:
        api_name = STANDARD_API_NAMES[dataset]
        return [
            _plan(
                dataset,
                api_name,
                {"ts_code": symbol, **_base_params(scope)},
                f"{dataset.value}:{symbol}",
            )
            for symbol in scope.symbols
        ]

    raise ValueError(f"unsupported Tushare dataset: {dataset}")
