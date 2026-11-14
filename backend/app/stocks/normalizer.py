from __future__ import annotations

from copy import deepcopy

from app.shared.boards import normalize_board
from app.stocks.derived.risk_assessment import assess_risk_checklist
from app.stocks.return_history import project_screener_roic
from app.shared.errors import DataServiceError
from app.shared.validation import require_object
from app.stocks.rea_fields import REA_PRESETS, REA_GROUPS, REA_BOARDS, REA_EXCLUSIONS, REA_THRESHOLD_UI, REA_SORT_FIELDS


def normalize_screener_universe(value: object) -> dict[str, object]:
    payload = require_object(value, {"bootstrap", "stocks"})
    bootstrap = payload["bootstrap"]
    stocks = payload["stocks"]
    if not isinstance(bootstrap, dict) or not isinstance(stocks, list):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约")
    if not all(isinstance(item, dict) for item in stocks):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约")
    normalized_stocks = deepcopy(stocks)
    for stock in normalized_stocks:
        project_screener_roic(stock)
        if stock.get('metricBasis',{}).get('realMoneyIndex')=='annual-OCF/parent-profit':
            stock.setdefault('researchValues',{}).setdefault('pePercentile',None)
            stock.setdefault('researchValues',{}).setdefault('pbPercentile',None)
        if "board" in stock:
            stock["board"] = normalize_board(
                stock["board"], exchange=stock.get("exchange"), symbol=stock.get("symbol"),
            )
    return {
        "bootstrap": {**deepcopy(bootstrap), "presets": deepcopy(REA_PRESETS), "groups": deepcopy(REA_GROUPS), "boards": deepcopy(REA_BOARDS), "exclusions": deepcopy(REA_EXCLUSIONS), "thresholds": deepcopy(REA_THRESHOLD_UI), "sortFields":deepcopy(REA_SORT_FIELDS)},
        "stocks": normalized_stocks,
    }


WORKBENCH_KEYS = {
    "symbol",
    "sidebar",
    "quote",
    "dailyChart",
    "metrics",
    "risks",
    "financial",
    "isFallbackSample",
}


def historical_workbench(value):
    return {**value, "risks": dict(historical=True, level="风险清单仅提供最新结果", score=None, groups=[])}


def normalize_workbench(value: object, *, assess_risks: bool = False) -> dict[str, object]:
    payload = require_object(value, WORKBENCH_KEYS)
    if assess_risks and payload["risks"] != historical_workbench({})["risks"]:
        payload["risks"] = assess_risk_checklist(deepcopy(payload["risks"]))
    return payload
