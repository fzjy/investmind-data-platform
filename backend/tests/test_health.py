from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from backend.tests.support.application import create_app as create_test_app


def test_fixture_health_keeps_existing_contract() -> None:
    client = TestClient(create_test_app())

    assert client.get("/health").json() == {"status": "ok"}


def test_database_health_reports_readiness(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'health.sqlite3'}"
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(data_backend="database", database_url=database_url),
    )

    client = TestClient(main_module.create_app())

    assert client.get("/health").json() == {"status": "ok", "database": "ready"}
