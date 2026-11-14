from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.tests.support.application import create_app
from app.shared.contracts import DataMeta, ProviderSnapshot


def default_request() -> dict[str, Any]:
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


def fixture_meta() -> DataMeta:
    return DataMeta(
        source="fixture",
        asOf="2026-08-15T00:00:00+00:00",
        version="test-v1",
        stale=False,
    )


def stock(
    symbol: str,
    *,
    dividend_yield: float | None,
    pe: float | None,
    industry: str,
    is_loss: bool = False,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": symbol,
        "price": 10,
        "change": 1,
        "marketCap": 100,
        "pe": pe,
        "pb": 2,
        "dividendYield": dividend_yield,
        "roe": 10,
        "grossMargin": 20,
        "netMargin": 10,
        "revenueGrowth": 5,
        "profitGrowth": 5,
        "realMoneyIndex": 1.2,
        "cashflowGrowth": 5,
        "dividendYears": 5,
        "payoutRatio": 30,
        "incomeSafety": 1,
        "industry": industry,
        "board": "sh-main",
        "isST": False,
        "isLoss": is_loss,
        "listingYears": 10,
        "pePercentile": 30,
        "pbPercentile": 20,
        "expectedDividendYield": 1,
        "capitalReturnRatio": 1,
        "roic": 8,
        "institutionHolding": 10,
        "majorIndustry": industry,
        "subIndustry": industry,
        "concepts": ["价值"],
        "region": "上海",
        "inventoryRisk": False,
        "netDebt": False,
    }


def three_stocks_snapshot() -> ProviderSnapshot[dict[str, object]]:
    stocks = [
        stock("600036.SH", dividend_yield=5, pe=8, industry="金融"),
        stock("000001.SZ", dividend_yield=2, pe=None, industry="金融"),
        stock("688525.SH", dividend_yield=7, pe=12, industry="电子", is_loss=True),
    ]
    return ProviderSnapshot(
        raw={"bootstrap": {"totalCount": len(stocks)}, "stocks": stocks},
        meta=fixture_meta(),
    )


@dataclass
class StaticStocksProvider:
    snapshot: ProviderSnapshot[dict[str, object]]

    def read_screener_universe(self) -> ProviderSnapshot[dict[str, object]]:
        return self.snapshot


@pytest.mark.parametrize(("exclusion", "field"), [("loss", "isLoss"), ("inventoryRisk", "inventoryRisk"), ("netDebt", "netDebt")])
def test_rea_exclusions_keep_unknown_without_labelling_safe(exclusion: str, field: str) -> None:
    from app.stocks.models import ScreenerRequest
    from app.stocks.service import ScreenerService

    rows = [stock(f"00000{index}.SZ", dividend_yield=2, pe=8, industry="行业") for index in range(3)]
    for row, value in zip(rows, (False, None, True)):
        row[field] = value
    request = default_request()
    request["filters"]["exclusions"] = [exclusion]
    snapshot = ProviderSnapshot(raw={"bootstrap": {}, "stocks": rows}, meta=fixture_meta())
    result = ScreenerService(StaticStocksProvider(snapshot)).query(ScreenerRequest.model_validate(request))
    assert [item["symbol"] for item in result.data["items"]] == ["000000.SZ", "000001.SZ"]
    assert rows[1][field] is None


def test_request_rejects_page_size_above_200() -> None:
    from app.stocks.models import ScreenerRequest

    payload = default_request()
    payload["pageSize"] = 201
    with pytest.raises(ValidationError):
        ScreenerRequest.model_validate(payload)


def test_request_rejects_unknown_sort_field() -> None:
    from app.stocks.models import ScreenerRequest

    payload = default_request()
    payload["sortRules"] = [{"field": "secret", "direction": "asc", "enabled": True}]
    with pytest.raises(ValidationError):
        ScreenerRequest.model_validate(payload)


def test_query_applies_threshold_category_sort_and_page() -> None:
    from app.stocks.models import ScreenerRequest
    from app.stocks.service import ScreenerService

    payload = default_request()
    payload["filters"]["thresholds"]["dividendYield"] = 4
    payload["filters"]["categories"]["majorIndustry"] = ["金融"]
    request = ScreenerRequest.model_validate(payload)
    result = ScreenerService(StaticStocksProvider(three_stocks_snapshot())).query(request)
    assert [item["symbol"] for item in result.data["items"]] == ["600036.SH"]
    assert result.data["total"] == 1


def test_sort_places_null_after_numeric_values() -> None:
    from app.stocks.models import ScreenerRequest
    from app.stocks.service import ScreenerService

    payload = default_request()
    payload["sortRules"] = [{"field": "pe", "direction": "asc", "enabled": True}]
    result = ScreenerService(StaticStocksProvider(three_stocks_snapshot())).query(
        ScreenerRequest.model_validate(payload)
    )
    assert [item["symbol"] for item in result.data["items"]][-1] == "000001.SZ"


def test_exclusion_filters_loss_stocks() -> None:
    from app.stocks.models import ScreenerRequest
    from app.stocks.service import ScreenerService

    payload = default_request()
    payload["filters"]["exclusions"] = ["loss"]
    result = ScreenerService(StaticStocksProvider(three_stocks_snapshot())).query(
        ScreenerRequest.model_validate(payload)
    )
    assert all(not item["isLoss"] for item in result.data["items"])


def test_screener_route_returns_fixture_results() -> None:
    response = TestClient(create_app()).post("/api/data/screener", json=default_request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["page"] == 1
    assert payload["data"]["pageSize"] == 200
    assert payload["meta"]["source"] == "fixture"


def test_screener_route_maps_validation_to_400() -> None:
    payload = default_request()
    payload["pageSize"] = 201
    response = TestClient(create_app()).post("/api/data/screener", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"

@pytest.mark.parametrize("payload", [default_request(), {"page": 0}, {"pageSize": 10}])
def test_versioned_screener_preserves_legacy_contract(payload: dict[str, Any]) -> None:
    client = TestClient(create_app())
    legacy = client.post("/api/data/screener", json=payload)
    versioned = client.post("/api/v1/stocks/screener", json=payload)
    assert versioned.status_code == legacy.status_code
    if legacy.status_code == 200:
        assert versioned.json() == legacy.json()
    else:
        assert versioned.json()["error"] == legacy.json()["error"]
        assert versioned.json()["meta"]["requestId"]
