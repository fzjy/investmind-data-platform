from __future__ import annotations

import time
from typing import Any

from app.shared.contracts import DataMeta, ProviderSnapshot
from app.stocks.models import ScreenerRequest
from app.stocks.service import ScreenerService


class GeneratedStocksProvider:
    def __init__(self, stocks: list[dict[str, object]]) -> None:
        self._snapshot = ProviderSnapshot(
            raw={"bootstrap": {"totalCount": len(stocks)}, "stocks": stocks},
            meta=DataMeta(
                source="fixture",
                asOf="2026-08-15T00:00:00+00:00",
                version="performance-v1",
                stale=False,
            ),
        )

    def read_screener_universe(self) -> ProviderSnapshot[dict[str, object]]:
        return self._snapshot


def request_payload() -> dict[str, Any]:
    return {
        "filters": {
            "preset": None,
            "thresholds": {
                "pe": 100,
                "pb": 20,
                "pePercentile": 100,
                "pbPercentile": 100,
                "dividendYield": 0,
                "expectedDividendYield": 0,
                "dividendYears": 0,
                "payoutRatio": 0,
                "capitalReturnRatio": 0,
                "roe": 0,
                "roic": 0,
                "realMoneyIndex": 0,
                "institutionHolding": 0,
                "marketCap": 0,
            },
            "exclusions": [],
            "enabledThresholds": [],
            "boards": ["sh-main", "sz-main", "star", "chinext", "bse"],
            "categories": {
                "majorIndustry": [],
                "subIndustry": [],
                "concept": [],
                "region": [],
            },
        },
        "sortRules": [
            {"field": "dividendYield", "direction": "desc", "enabled": True},
        ],
        "page": 1,
        "pageSize": 200,
    }


def generated_stocks(count: int) -> list[dict[str, object]]:
    stocks: list[dict[str, object]] = []
    for index in range(count):
        stock: dict[str, object] = {
            "symbol": f"{index % 1_000_000:06d}.SH",
            "name": f"Generated {index}",
            "price": float(index % 100) + 1,
            "change": float(index % 10),
            "marketCap": float(index + 1),
            "pe": float(index % 80) + 1,
            "pb": float(index % 10) + 1,
            "dividendYield": float(index % 10),
            "roe": float(index % 30),
            "grossMargin": 20.0,
            "netMargin": 10.0,
            "revenueGrowth": 5.0,
            "profitGrowth": 5.0,
            "realMoneyIndex": 1.2,
            "cashflowGrowth": 5.0,
            "dividendYears": 5,
            "payoutRatio": 30.0,
            "incomeSafety": 1.0,
            "industry": "测试",
            "board": "sh-main",
            "isST": False,
            "isLoss": False,
            "listingYears": 5.0,
            "pePercentile": 30.0,
            "pbPercentile": 20.0,
            "expectedDividendYield": 1.0,
            "capitalReturnRatio": 1.0,
            "roic": 8.0,
            "institutionHolding": 10.0,
            "majorIndustry": "测试",
            "subIndustry": "测试",
            "concepts": ["演示"],
            "region": "上海",
            "inventoryRisk": False,
            "netDebt": False,
        }
        stock.update({f"supplierField{column}": index for column in range(118)})
        assert len(stock) == 153
        stocks.append(stock)
    return stocks


def test_screener_handles_6566_rows_and_153_fields_under_ten_seconds() -> None:
    stocks = generated_stocks(6_566)
    request = ScreenerRequest.model_validate(request_payload())
    started = time.perf_counter()
    result = ScreenerService(GeneratedStocksProvider(stocks)).query(request)
    elapsed = time.perf_counter() - started
    assert result.data["total"] == 6_566
    assert len(result.data["items"]) == 200
    assert elapsed < 10
