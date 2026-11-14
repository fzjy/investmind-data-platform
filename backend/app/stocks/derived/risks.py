from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable


@dataclass(frozen=True)
class RiskItem:
    id: str
    status: str
    formula: str
    inputs: dict[str, Any]
    missing_fields: tuple[str, ...]
    conclusion: str


@dataclass(frozen=True)
class RiskSummary:
    score: Decimal | None
    coverage: Decimal
    items: tuple[RiskItem, ...]


@dataclass(frozen=True)
class RiskContext:
    values: dict[str, Any]
    as_of: str | None = None


def score_risks(items: Iterable[RiskItem]) -> RiskSummary:
    records = tuple(items)
    known = [item for item in records if item.status in {"risk", "normal"}]
    coverage = (Decimal(len(known)) / Decimal(len(records))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if records else Decimal("0")
    if not records or coverage < Decimal("0.6"):
        return RiskSummary(None, coverage, records)
    score = (Decimal(sum(item.status == "normal" for item in known)) / Decimal(len(known)) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return RiskSummary(score, coverage, records)


def _item(risk_id: str, status: str, formula: str, inputs: dict[str, Any], missing: tuple[str, ...] = ()) -> RiskItem:
    if status == "risk":
        conclusion = f"{risk_id}: 触发风险阈值"
    elif status == "normal":
        conclusion = f"{risk_id}: 未触发风险阈值"
    else:
        conclusion = f"{risk_id}: 数据不足，无法判断"
    return RiskItem(risk_id, status, formula, inputs, missing, conclusion)


def _threshold_item(risk_id: str, value: Any, threshold: Decimal, formula: str, missing_name: str) -> RiskItem:
    if value is None:
        return _item(risk_id, "unknown", formula, {}, (missing_name,))
    try:
        numeric = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return _item(risk_id, "unknown", formula, {}, (missing_name,))
    return _item(risk_id, "risk" if numeric >= threshold else "normal", formula, {"value": numeric, "threshold": threshold})


def evaluate_risks(context: RiskContext | dict[str, Any]) -> RiskSummary:
    values = context.values if isinstance(context, RiskContext) else context
    items = [
        _threshold_item("inventoryRisk", values.get("inventory_ratio"), Decimal("0.4"), "inventory / revenue >= 0.4", "inventory_ratio"),
        _threshold_item("goodwillRisk", values.get("goodwill_ratio"), Decimal("0.3"), "goodwill / assets >= 0.3", "goodwill_ratio"),
        _threshold_item("netDebtRisk", values.get("net_debt_ratio"), Decimal("1"), "net debt / equity >= 1", "net_debt_ratio"),
        _threshold_item("cashProfitRisk", values.get("real_money_index"), Decimal("0.8"), "CFO / net profit < 0.8", "real_money_index"),
        _threshold_item("valuationRisk", values.get("pe_percentile"), Decimal("90"), "PE percentile >= 90", "pe_percentile"),
        _threshold_item("dividendRisk", values.get("payout_ratio"), Decimal("0"), "payout ratio <= 0", "payout_ratio"),
        _threshold_item("cashDividendRisk", values.get("cashflow_dividend_ratio"), Decimal("1"), "CFO / dividend < 1", "cashflow_dividend_ratio"),
    ]
    # A payout ratio of zero is a risk; positive payout is normal. The helper above
    # treats >= as risk, so normalize this special rule explicitly.
    payout = values.get("payout_ratio")
    if payout is not None:
        items[5] = _item("dividendRisk", "risk" if Decimal(str(payout)) <= 0 else "normal", "payout ratio <= 0", {"payout_ratio": payout})
    cash_profit = values.get("real_money_index")
    if cash_profit is not None:
        numeric = Decimal(str(cash_profit))
        items[3] = _item("cashProfitRisk", "risk" if numeric < Decimal("0.8") else "normal", "CFO / net profit < 0.8", {"real_money_index": numeric, "threshold": Decimal("0.8")})
    return score_risks(items)


__all__ = ["RiskContext", "RiskItem", "RiskSummary", "evaluate_risks", "score_risks"]
