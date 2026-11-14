from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.config import Settings
from app.db.base import Base, DerivedBase, MartBase
from app.db.models import PublishedSnapshot


def test_database_routes_read_published_snapshot(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'api.sqlite3'}"
    engine = create_engine(database_url)
    bind = engine.execution_options(schema_translate_map={"raw": None, "core": None, "ops": None, "derived": None, "mart": None})
    Base.metadata.create_all(bind)
    DerivedBase.metadata.create_all(bind)
    MartBase.metadata.create_all(bind)
    with sessionmaker(bind).begin() as session:
        session.add(PublishedSnapshot(product="market-overview", version="v1", as_of=datetime(2026, 8, 17, tzinfo=timezone.utc), payload={"marketSnapshot": {}, "marketStatuses": [], "globalIndices": [], "turnoverSnapshot": {}, "turnoverTop10": [], "limitStats": {}, "sentimentSnapshot": {}, "fundFlowSnapshot": {}, "breadthSnapshots": [], "etfQuotes": []}))
    monkeypatch.setattr(main_module, "settings", Settings(data_backend="database", database_url=database_url))
    response = TestClient(main_module.create_app()).get("/api/data/market-overview")
    assert response.status_code == 200
    assert response.json()["meta"]["source"] == "database"


def test_database_routes_return_not_ready_without_snapshot(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'empty.sqlite3'}"
    engine = create_engine(database_url)
    bind = engine.execution_options(schema_translate_map={"raw": None, "core": None, "ops": None, "derived": None, "mart": None})
    Base.metadata.create_all(bind)
    DerivedBase.metadata.create_all(bind)
    MartBase.metadata.create_all(bind)
    monkeypatch.setattr(main_module, "settings", Settings(data_backend="database", database_url=database_url))
    response = TestClient(main_module.create_app()).get("/api/data/market-overview")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_NOT_READY"
