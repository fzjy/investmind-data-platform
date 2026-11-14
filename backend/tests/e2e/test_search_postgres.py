"""登记一次性PG，搜索只消费当前发布指针，不回退历史或证券主数据。"""
from datetime import date, datetime, timezone
import os

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

import app.main as main_module
from app.config import PROJECT_ROOT, Settings
from app.db.models import PublishedSnapshot, Security, StockScreenerDaily
from app.marts.contracts import MartCandidate
from app.marts.publication import SnapshotPublisher
from app.ops.backup.inventory import read_inventory, snapshot_inventory


pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
    reason="requires registered disposable PostgreSQL17")]


@pytest.fixture
def database(monkeypatch):
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        settings = Settings(_env_file=None, data_backend="database", database_url=url)
        monkeypatch.setenv("DANG_DATABASE_URL", url)
        command.upgrade(Config(str(PROJECT_ROOT / "backend/alembic.ini")), "head")
        engine = create_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(main_module, "settings", settings)
        try:
            yield factory, TestClient(main_module.create_app())
        finally:
            engine.dispose()


def publish(factory, version, day, stocks):
    SnapshotPublisher(factory).publish_many((MartCandidate(product="screener",
        as_of=datetime(2026, 10, day, tzinfo=timezone.utc), payload={"bootstrap": {}, "stocks": stocks}),), version, ())


def inventory(factory):
    with snapshot_inventory(factory.kw["bind"], timeout=30) as view:
        return read_inventory(view.connection, timeout=30)


def test_search_never_falls_back_to_historical_rows(database):
    factory, client = database
    with factory.begin() as session:
        session.add_all([Security(ts_code=code, symbol=code[:6], name="主数据", status="L", data={})
                         for code in ("600519.SH", "000001.SZ", "600999.SH")])
    publish(factory, "old", 12, [{"symbol": "000001.SZ", "name": "旧平安银行"}])
    publish(factory, "current", 13, [{"symbol": "600519.SH", "name": "贵州茅台", "isST": True}])
    before = inventory(factory)
    response = client.get("/api/data/search?q=600519")
    assert response.status_code == 200
    body = response.json()["data"]["stocks"]
    assert [item["symbol"] for item in body["items"]] == ["600519.SH"]
    assert body["meta"]["source"] == "database" and body["meta"]["version"] == "current"
    assert client.get("/api/data/search?q=000001").json()["data"]["stocks"]["total"] == 0
    assert client.get("/api/data/search?q=600999").json()["data"]["stocks"]["total"] == 0
    assert inventory(factory) == before


def test_unpublished_structured_rows_are_not_search_results(database):
    factory, client = database
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="主数据", status="L", data={}))
        session.flush()
        session.add(StockScreenerDaily(ts_code="600519.SH", as_of=date(2026, 10, 13),
            snapshot_version="legacy", fields={"symbol": "600519.SH", "name": "旧贵州茅台"}))
    before = inventory(factory)
    response = client.get("/api/data/search?q=600519")
    assert response.status_code == 200
    body = response.json()["data"]["stocks"]
    assert body["status"] == "unavailable" and body["error"]["code"] == "DATA_NOT_READY"
    assert body["total"] is None and body["meta"] is None
    assert inventory(factory) == before


def test_real_empty_publication_is_distinct_from_no_publication(database):
    factory, client = database
    publish(factory, "empty", 13, [])
    before = inventory(factory)
    body = client.get("/api/data/search?q=股票").json()["data"]
    assert body["stocks"]["status"] == "ready" and body["stocks"]["total"] == 0
    assert body["stocks"]["meta"]["version"] == "empty"
    assert set(body) == {"query","stocks"}
    assert inventory(factory) == before
