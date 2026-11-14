from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.tests.support.application import create_app
from app.shared.errors import DataServiceError
from backend.tests.support.fixtures import FixtureStore


def fixture_provider():
    from backend.tests.support.providers import StocksFixtureProvider

    return StocksFixtureProvider(FixtureStore.default())


def test_workbench_returns_supported_symbol_without_fallback() -> None:
    from app.stocks.service import WorkbenchService

    result = WorkbenchService(fixture_provider()).get_workbench("688525.sh")
    assert result.data["quote"]["symbol"] == "688525.SH"
    assert result.data["isFallbackSample"] is False


def test_workbench_rejects_invalid_symbol() -> None:
    from app.stocks.service import WorkbenchService

    with pytest.raises(DataServiceError) as raised:
        WorkbenchService(fixture_provider()).get_workbench("bad")
    assert raised.value.code == "INVALID_REQUEST"


def test_workbench_returns_not_found_for_valid_unknown_symbol() -> None:
    from app.stocks.service import WorkbenchService

    with pytest.raises(DataServiceError) as raised:
        WorkbenchService(fixture_provider()).get_workbench("000000.SH")
    assert raised.value.code == "DATA_NOT_FOUND"


def test_workbench_route_returns_known_symbol() -> None:
    response = TestClient(create_app()).get("/api/data/stocks/688525.SH")
    assert response.status_code == 200
    assert response.json()["data"]["quote"]["symbol"] == "688525.SH"


@pytest.mark.parametrize(
    ("symbol", "status", "code"),
    [("bad", 400, "INVALID_REQUEST"), ("000000.SH", 404, "DATA_NOT_FOUND")],
)
def test_workbench_route_errors(symbol: str, status: int, code: str) -> None:
    response = TestClient(create_app()).get(f"/api/data/stocks/{symbol}")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
