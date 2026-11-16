from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from backend.tests.support.application import create_app as create_test_app


def test_fixture_health_keeps_existing_contract() -> None:
    client = TestClient(create_test_app())

    assert client.get("/health").json() == {"status": "ok"}


def test_database_health_reports_readiness(monkeypatch, pg_url) -> None:
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(data_backend="database", database_url=pg_url),
    )

    client = TestClient(main_module.create_app())

    assert client.get("/health").json() == {"status": "ok", "database": "ready"}


def test_read_api_starts_no_news_collection_by_default(monkeypatch):
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from app.content import news_worker
    from app.main import create_app

    collector = AsyncMock()
    monkeypatch.setattr(news_worker, "news_loop", collector)
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200
    collector.assert_not_awaited()
