from __future__ import annotations

from copy import deepcopy
from functools import cmp_to_key
from math import isfinite, sqrt
from app.shared.contracts import DataResult, success_result
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol
from app.stocks.definitions import THRESHOLD_DEFINITIONS, LEGACY_DEFAULTS
from app.stocks.rea_fields import REA_THRESHOLD_UI
from app.stocks.models import ScreenerFilters, ScreenerRequest
from app.stocks.normalizer import normalize_screener_universe, normalize_workbench
from app.stocks.provider import StocksProvider


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) else None


def _matches_thresholds(stock: dict[str, object], filters: ScreenerFilters) -> bool:
    for key, definition in THRESHOLD_DEFINITIONS.items():
        selected = getattr(filters.thresholds, definition.attribute)
        if (filters.enabled_thresholds is not None and key not in filters.enabled_thresholds) or (filters.enabled_thresholds is None and selected == LEGACY_DEFAULTS.get(key, definition.default)):
            continue
        extra = stock.get("researchValues") if isinstance(stock.get("researchValues"), dict) else {}
        value = _number(extra[key] if key in extra else stock.get(key))
        if value is None or (key in {"pe", "pb"} and value <= 0):
            return False
        direction = filters.threshold_directions.get(key, definition.direction)
        if not {"gt": value > selected, "lt": value < selected, "gte": value >= selected, "lte": value <= selected}[direction]:
            return False
    return True


def _matches_exclusions(stock: dict[str, object], exclusions: list[str]) -> bool:
    for exclusion in exclusions:
        if exclusion == "loss" and (stock.get("isLoss") is True or (_number(stock.get("pe")) is not None and stock["pe"] < 0)):
            return False
        if exclusion == "st" and (stock.get("isST") or 'ST' in str(stock.get('name',''))):
            return False
        if exclusion == "inventoryRisk" and stock.get("inventoryRisk") is True:
            return False
        if exclusion == "lowRealMoney" and _number(stock.get("realMoneyIndex")) is not None and stock["realMoneyIndex"] < .8:
            return False
        if exclusion == "netDebt" and stock.get("netDebt") is True:
            return False
        if exclusion == "performanceDecline":
            points = [_number(v) for v in stock.get("profitTrend", [])] if isinstance(stock.get("profitTrend"), list) else []
            points = [v for v in points if v is not None]
            if len(points) >= 2 and all(b < a for a, b in zip(points, points[1:])):
                return False
        if exclusion == "noDividend":
            payouts = stock.get("payoutRatios")
            if isinstance(payouts, list) and len(payouts) == 3 and all(_number(v) == 0 for v in payouts):
                return False
        if exclusion == "revenueDecline" and _number(stock.get("revenueGrowth")) is not None and stock["revenueGrowth"] <= 0:
            return False
        if exclusion == "cashflowDecline" and _number(stock.get("cashflowGrowth")) is not None and stock["cashflowGrowth"] <= 0:
            return False
    return True


def _matches_categories(stock: dict[str, object], filters: ScreenerFilters) -> bool:
    stock_categories = {
        "majorIndustry": [stock.get("majorIndustry")],
        "subIndustry": [stock.get("subIndustry")],
        "concept": stock.get("concepts") if isinstance(stock.get("concepts"), list) else [],
        "region": [stock.get("region")],
    }
    category_values = filters.categories.model_dump(by_alias=True)
    for category, selections in category_values.items():
        if selections and not any(selection in stock_categories[category] for selection in selections):
            return False
    return True


def _compare_values(left: object, right: object, direction: str) -> int:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None and right_number is None:
        return 0
    if left_number is None:
        return 1
    if right_number is None:
        return -1
    if left_number == right_number:
        return 0
    if direction == "asc":
        return -1 if left_number < right_number else 1
    return -1 if left_number > right_number else 1


def _sort_stocks(stocks: list[dict[str, object]], request: ScreenerRequest) -> list[dict[str, object]]:
    active_rules = [rule for rule in request.sort_rules if rule.enabled and rule.weight > 0]
    def value(stock, field):
        extra=stock.get("researchValues") if isinstance(stock.get("researchValues"),dict) else {}
        return _number(extra[field] if field in extra else stock.get(field))
    if request.sort_mode == "advanced" and active_rules:
        stats=[]
        for rule in active_rules:
            values=[value(s,rule.field) for s in stocks]
            values=[v for v in values if v is not None and (rule.field not in {"pe","pb"} or v>0)]
            mean=sum(values)/len(values) if values else 0
            sd=sqrt(sum((v-mean)**2 for v in values)/(len(values)-1)) if len(values)>1 else 0
            zs=[(v-mean)/sd if sd>0 else 0 for v in values]
            worst=max([0]+zs)+1 if rule.direction=='asc' else min([0]+zs)-1
            stats.append((mean,sd,worst))
        def score(stock):
            total=0;coverage=0
            for rule,(mean,sd,worst) in zip(active_rules,stats):
                v=value(stock,rule.field)
                if v is None:continue
                coverage+=1
                z=worst if rule.field in {'pe','pb'} and v<=0 else (v-mean)/sd if sd>0 else 0
                total+=rule.weight*z*(-1 if rule.direction=='asc' else 1)
            return total*coverage/len(active_rules) if coverage else float('-inf')
        return sorted(stocks,key=lambda s:(-score(s),str(s.get('symbol',''))))

    def compare(left: dict[str, object], right: dict[str, object]) -> int:
        for rule in active_rules:
            left_value=value(left,rule.field);right_value=value(right,rule.field)
            if rule.field in {'pe','pb'}:
                left_value=left_value if left_value is not None and left_value>0 else None
                right_value=right_value if right_value is not None and right_value>0 else None
            result = _compare_values(left_value, right_value, rule.direction)
            if result:
                return result
        return -1 if str(left.get("symbol", "")) < str(right.get("symbol", "")) else 1 if str(left.get("symbol", "")) > str(right.get("symbol", "")) else 0

    return sorted(stocks, key=cmp_to_key(compare))


class ScreenerService:
    def __init__(self, provider: StocksProvider) -> None:
        self._provider = provider

    def query(self, request: ScreenerRequest) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_screener_universe()
        universe = normalize_screener_universe(snapshot.raw)
        stocks = [item for item in universe["stocks"] if isinstance(item, dict)]
        filters = request.filters
        bank=None
        if request.bank is not None:
            from app.stocks.bank import project_bank,bank_matches
            from app.stocks.bank_scoring import rules
            settings=request.bank.model_dump(by_alias=True)
            stocks=[dict(item,bankEvaluation=project_bank(item,snapshot.meta.as_of,settings)) for item in stocks if item.get('majorIndustry')=='银行']
            bank=dict(algorithmVersion=rules()['algorithmVersion'],universeCount=len(stocks),settings=settings,
                availableCounts={f['field']:sum(item['bankEvaluation']['fields'][f['field']] is not None for item in stocks) for f in rules()['fields']})
            bank['selection']=dict(boards=list(filters.boards),exclusions=list(filters.exclusions),
                subIndustry=list(filters.categories.sub_industry),concept=list(filters.categories.concept),region=list(filters.categories.region))
            filters=filters.model_copy(update={'categories':filters.categories.model_copy(update={'major_industry':[]})})
        filtered = [
            item
            for item in stocks
            if _matches_exclusions(item, list(filters.exclusions))
            and item.get("board") in filters.boards
            and _matches_categories(item, filters)
            and (bank_matches(item['bankEvaluation'],request.bank.conditions) if request.bank is not None else _matches_thresholds(item, filters))
        ]
        if request.bank is not None:
            ordered=sorted(filtered,key=lambda item: ((item['bankEvaluation']['total'] is None,-item['bankEvaluation']['total'] if item['bankEvaluation']['total'] is not None else 0,str(item.get('symbol',''))) if request.bank.sort=='score-desc' else (str(item.get('symbol','')),)))
        else:ordered = _sort_stocks(filtered, request)
        start = (request.page - 1) * request.page_size
        counts={}
        for key in THRESHOLD_DEFINITIONS:
            count=0
            for stock in stocks:
                extra=stock.get('researchValues') if isinstance(stock.get('researchValues'),dict) else {}
                value=_number(extra[key] if key in extra else stock.get(key))
                if value is not None and (key not in {'pe','pb'} or value>0):count+=1
            counts[key]=count
        return success_result(
            {
                "bootstrap": {**deepcopy(universe["bootstrap"]), "thresholds": deepcopy(REA_THRESHOLD_UI), "availableCounts":counts},
                "items": deepcopy(ordered[start : start + request.page_size]),
                "total": len(ordered),
                "page": request.page,
                "pageSize": request.page_size,
                "snapshot": {"asOf": snapshot.meta.as_of, "version": snapshot.meta.version},
                "unknownExclusionsRetained": bool(filters.exclusions),
                **({'bank':bank} if bank is not None else {}),
            },
            snapshot.meta,
        )


class WorkbenchService:
    def __init__(self, provider: StocksProvider) -> None:
        self._provider = provider

    def get_workbench(self, symbol_input: str) -> DataResult[dict[str, object]]:
        symbol = normalize_stock_symbol(symbol_input)
        snapshot = self._provider.read_workbench(symbol)
        if snapshot.raw is None:
            raise DataServiceError("DATA_NOT_FOUND", f"未找到股票 {symbol} 的工作台数据")
        data = normalize_workbench(snapshot.raw, assess_risks=snapshot.meta.source == "database")
        data["isFallbackSample"] = False
        return success_result(deepcopy(data), snapshot.meta)
