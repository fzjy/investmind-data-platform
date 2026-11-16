from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.db.models import PublishedSnapshot


def test_database_routes_read_published_snapshot(monkeypatch, pg_schema_url, pg_factory) -> None:
    with pg_factory.begin() as session:
        session.add(PublishedSnapshot(product="market-overview", version="v1", as_of=datetime(2026, 8, 17, tzinfo=timezone.utc), payload={"marketSnapshot": {}, "marketStatuses": [], "globalIndices": [], "turnoverSnapshot": {}, "turnoverTop10": [], "limitStats": {}, "sentimentSnapshot": {}, "fundFlowSnapshot": {}, "breadthSnapshots": [], "etfQuotes": []}))
    monkeypatch.setattr(main_module, "settings", Settings(data_backend="database", database_url=pg_schema_url))
    response = TestClient(main_module.create_app()).get("/api/data/market-overview")
    assert response.status_code == 200
    assert response.json()["meta"]["source"] == "database"


def test_database_routes_return_not_ready_without_snapshot(monkeypatch, pg_schema_url) -> None:
    monkeypatch.setattr(main_module, "settings", Settings(data_backend="database", database_url=pg_schema_url))
    response = TestClient(main_module.create_app()).get("/api/data/market-overview")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_NOT_READY"
