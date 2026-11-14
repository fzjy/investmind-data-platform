from __future__ import annotations

from dataclasses import dataclass
from app.stocks.rea_fields import REA_THRESHOLD_ROWS


@dataclass(frozen=True)
class ThresholdDefinition:
    attribute: str
    stock_key: str
    direction: str
    default: float


THRESHOLD_DEFINITIONS: dict[str, ThresholdDefinition] = {
    "pe": ThresholdDefinition("pe", "pe", "lte", 100),
    "pb": ThresholdDefinition("pb", "pb", "lte", 20),
    "pePercentile": ThresholdDefinition("pe_percentile", "pePercentile", "lte", 100),
    "pbPercentile": ThresholdDefinition("pb_percentile", "pbPercentile", "lte", 100),
    "dividendYield": ThresholdDefinition("dividend_yield", "dividendYield", "gte", 0),
    "expectedDividendYield": ThresholdDefinition(
        "expected_dividend_yield", "expectedDividendYield", "gte", 0
    ),
    "dividendYears": ThresholdDefinition("dividend_years", "dividendYears", "gte", 0),
    "payoutRatio": ThresholdDefinition("payout_ratio", "payoutRatio", "gte", 0),
    "capitalReturnRatio": ThresholdDefinition(
        "capital_return_ratio", "capitalReturnRatio", "gte", 0
    ),
    "roe": ThresholdDefinition("roe", "roe", "gte", 0),
    "roic": ThresholdDefinition("roic", "roic", "gte", 0),
    "realMoneyIndex": ThresholdDefinition("real_money_index", "realMoneyIndex", "gte", 0),
    "institutionHolding": ThresholdDefinition(
        "institution_holding", "institutionHolding", "gte", 0
    ),
    "marketCap": ThresholdDefinition("market_cap", "marketCap", "gte", 0),
}

# 原默认值只供读取旧请求；新客户端始终显式传enabledThresholds。
LEGACY_DEFAULTS = {key: value.default for key, value in THRESHOLD_DEFINITIONS.items()}
THRESHOLD_DEFINITIONS = {key: ThresholdDefinition(attribute, key, direction, default) for key, attribute, direction, default in REA_THRESHOLD_ROWS}
