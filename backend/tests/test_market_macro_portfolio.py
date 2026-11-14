from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.tests.support.application import create_app
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError


def fixture_meta() -> DataMeta:
    return DataMeta(
        source="fixture",
        asOf="2026-08-15T00:00:00+00:00",
        version="test-v1",
        stale=False,
    )


@dataclass
class StaticMarketProvider:
    overview: dict[str, object]
    explorer: dict[str, object]
    meta: DataMeta

    def read_overview(self) -> ProviderSnapshot[dict[str, object]]:
        return ProviderSnapshot(self.overview, self.meta)

    def read_explorer(self) -> ProviderSnapshot[dict[str, object]]:
        return ProviderSnapshot(self.explorer, self.meta)


def test_market_service_normalizes_required_payload() -> None:
    from app.market.service import MarketService

    provider = StaticMarketProvider(
        overview={
            "marketSnapshot": {},
            "marketStatuses": [],
            "globalIndices": [],
            "turnoverSnapshot": {},
            "turnoverTop10": [],
            "limitStats": {},
            "sentimentSnapshot": {},
            "fundFlowSnapshot": {},
            "breadthSnapshots": [],
            "etfQuotes": [],
        },
        explorer={
            "commodityCategories": [],
            "commodityData": {},
            "commodityPeriods": [],
            "heatmapCategories": [],
            "heatmapData": [],
            "marketNews": [],
            "rankingDecliners": [],
            "rankingLeaders": [],
            "snapshotLabel": "",
        },
        meta=fixture_meta(),
    )
    result = MarketService(provider).get_overview()
    assert result.data["marketStatuses"] == []
    assert result.meta.source == "fixture"


def test_market_normalizer_rejects_missing_market_snapshot() -> None:
    from app.market.normalizer import normalize_market_overview

    with pytest.raises(DataServiceError) as raised:
        normalize_market_overview({"marketStatuses": [], "globalIndices": []})
    assert raised.value.code == "INVALID_SOURCE_PAYLOAD"


def test_macro_transmission_requires_both_sections() -> None:
    from app.macro.normalizer import normalize_macro_transmission

    with pytest.raises(DataServiceError):
        normalize_macro_transmission({"transmission": {}})


def test_public_portfolio_needs_business_date_and_excludes_old_website_state() -> None:
    from app.portfolio.normalizer import legacy_portfolio, normalize_portfolio

    with pytest.raises(DataServiceError):
        normalize_portfolio({"assets": [], "prices": {}, "defaultState": {}})
    public = {"assets": [], "prices": {}, "snapshotLabel": "2026-10-21"}
    old = {**public, "storageKey": "old", "defaultState": {
        "period": "1m", "positions": [{"assetId": "A", "weight": 100}], "capital": 23,
    }}
    assert normalize_portfolio(old) == normalize_portfolio(public) == public
    legacy = legacy_portfolio(old, "database")
    assert legacy["storageKey"] == "old"
    assert legacy["defaultState"] == {
        "period": "1m", "positions": [], "schemaVersion": 1,
        "strategy": "buy-and-hold", "weightMode": "manual",
    }
    assert old["defaultState"]["positions"]
    assert legacy_portfolio(public, "database")["defaultState"]["period"] == "3y"
    assert legacy_portfolio(public, "fixture")["defaultState"]["period"] == "1y"


@pytest.mark.parametrize(
    ("path", "required_key"),
    [
        ("/api/data/market-overview", "marketSnapshot"),
        ("/api/data/market-explorer", "marketNews"),
        ("/api/data/macro-transmission", "transmission"),
        ("/api/data/macro-insights", "statusCards"),
        ("/api/data/portfolio", "assets"),
        ("/api/v1/portfolio", "assets"),
    ],
)
def test_read_routes_return_stable_envelope(path: str, required_key: str) -> None:
    response = TestClient(create_app()).get(path)
    assert response.status_code == 200
    payload = response.json()
    assert required_key in payload["data"]
    assert payload["meta"]["source"] == "fixture"
    assert payload["meta"]["stale"] is False
