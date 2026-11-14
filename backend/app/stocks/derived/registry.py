from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.stocks.derived.formulas import (
    MetricResult,
    capital_return_ratio,
    dividend_years,
    expected_dividend_yield,
    free_cash_flow,
    historical_percentile,
    institution_holding,
    net_debt,
    payout_ratio,
    real_money_index,
    roic,
    yoy_growth,
)


MetricFunction = Callable[..., MetricResult]


@dataclass(frozen=True)
class FormulaDefinition:
    metric_id: str
    version: str
    inputs: tuple[str, ...]
    calculate: MetricFunction
    applicable_industries: tuple[str, ...] = ()


FORMULAS: dict[str, FormulaDefinition] = {
    "realMoneyIndex": FormulaDefinition("realMoneyIndex", "1.0.0", ("cfo_ttm", "parent_profit_ttm"), real_money_index),
    "freeCashFlow": FormulaDefinition("freeCashFlow", "1.0.0", ("cfo_ttm", "capex_ttm"), free_cash_flow),
    "roic": FormulaDefinition("roic", "1.0.0", ("nopat", "invested_capital"), roic),
    "pePercentile": FormulaDefinition("pePercentile", "1.0.0", ("pe", "pe_history_3y"), historical_percentile),
    "pbPercentile": FormulaDefinition("pbPercentile", "1.0.0", ("pb", "pb_history_3y"), historical_percentile),
    "dividendYears": FormulaDefinition("dividendYears", "2.0.0", ("dividend_fiscal_years",), dividend_years),
    "payoutRatio": FormulaDefinition("payoutRatio", "2.0.0", ("cash_dividend", "parent_profit"), payout_ratio),
    "capitalReturnRatio": FormulaDefinition("capitalReturnRatio", "2.0.0", ("dividends", "buybacks", "equity_financing_inflow"), capital_return_ratio),
    "expectedDividendYield": FormulaDefinition("expectedDividendYield", "2.0.0", ("latest_dps", "dividend_history", "close"), expected_dividend_yield),
    "institutionHolding": FormulaDefinition("institutionHolding", "2.0.0", ("institution_shares", "institution_total_shares"), institution_holding),
    "netDebt": FormulaDefinition("netDebt", "1.0.0", ("total_debt", "cash"), net_debt),
    "profitGrowth": FormulaDefinition("profitGrowth", "1.0.0", ("profit", "previous_profit"), yoy_growth),
    "revenueGrowth": FormulaDefinition("revenueGrowth", "1.0.0", ("revenue", "previous_revenue"), yoy_growth),
    "cashflowGrowth": FormulaDefinition("cashflowGrowth", "1.0.0", ("cfo", "previous_cfo"), yoy_growth),
}


def calculate_formula(metric_id: str, inputs: Mapping[str, Any]) -> MetricResult:
    definition = FORMULAS[metric_id]
    kwargs = {key: inputs.get(key) for key in definition.inputs}
    # percentile and dividend functions accept iterable/history inputs as named args.
    if metric_id in {"pePercentile", "pbPercentile"}:
        return definition.calculate(kwargs[definition.inputs[0]], kwargs[definition.inputs[1]])
    if metric_id == "dividendYears":
        return definition.calculate(kwargs["dividend_fiscal_years"], inputs.get("as_of_year"))
    aliases = {
        "cfo_ttm": "cfo",
        "parent_profit_ttm": "parent_profit",
        "capex_ttm": "capex",
        "profit": "current",
        "previous_profit": "previous",
        "revenue": "current",
        "previous_revenue": "previous",
        "cfo": "current",
        "previous_cfo": "previous",
    }
    return definition.calculate(**{aliases.get(key, key): value for key, value in kwargs.items()})
