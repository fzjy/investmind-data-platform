"""仅显式隔离 PG 测试；普通 pytest 不生成全规模行情。"""
import importlib
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import Settings

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="需要显式登记的独立 PostgreSQL17")]


def capacity():
    assert importlib.util.find_spec("app.ops.capacity") is not None, "缺少实际容量基准"
    return importlib.import_module("app.ops.capacity")


def test_real_builder_api_size_indexes_and_readonly_transaction(capsys):
    module = capacity()
    options = module.CapacityOptions(stock_count=2, trading_days=3, batch_size=2, api_samples=2)
    # 明确恶意业务 URL 必须被 benchmark 完全忽略。
    settings = SimpleNamespace(database_url="postgresql://u:secret-canary@business/db")
    report = module.capacity_benchmark(settings, options=options)
    print(json.dumps({key: report[key] for key in ("source", "scale", "insert", "build", "api", "budgets", "cleanupVerified")} | {
        "databaseBytes": report["database"]["databaseBytes"],
        "priceTable": next(row for row in report["database"]["tables"] if row["name"] == "daily_price"),
    }, ensure_ascii=False))
    assert report["source"] == "synthetic"
    assert report["scale"]["dailyPriceRows"] == 6
    assert report["scale"]["securityRows"] == 2
    assert report["build"]["screenerRows"] == 2
    assert report["build"]["loadedPriceRows"] == 2
    assert report["database"]["databaseBytes"] > 0
    assert any(row["name"] == "uq_daily_price_security_date" and row["bytes"] > 0 for row in report["database"]["indexes"])
    assert all(item["samples"] == 2 and item["total"] == 2 and item["p95Ms"] > 0 for item in report["api"])
    assert report["cleanupVerified"] is True
    assert "secret-canary" not in json.dumps(report)
    # 输出真实测量值，供主 agent 读取；不硬编码吞吐量。
    output = capsys.readouterr().out
    print(output)

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(driver="psycopg"))
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE capacity_canary (id integer)"))
            with module.readonly_connection(engine, timeout_seconds=2) as connection:
                assert connection.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
                assert int(connection.execute(text("SELECT setting FROM pg_settings WHERE name='statement_timeout'")).scalar_one()) <= 2000
                with pytest.raises(DBAPIError):
                    connection.execute(text("INSERT INTO capacity_canary VALUES (1)"))
            with module.readonly_connection(engine, timeout_seconds=0.05) as connection:
                with pytest.raises(DBAPIError):
                    connection.execute(text("SELECT pg_sleep(0.2)"))
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM capacity_canary")).scalar_one() == 0
        finally:
            engine.dispose()


def test_root_dotenv_benchmark_really_inserts_six_prices(tmp_path, monkeypatch):
    module = capacity()
    dotenv = tmp_path / ".env"
    dotenv.write_text("DANG_CAPACITY_STOCK_COUNT=2\nDANG_CAPACITY_TRADING_DAYS=3\nDANG_CAPACITY_BATCH_SIZE=2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", dotenv)
    settings = Settings.from_env()
    assert getattr(settings, "capacity_stock_count", None) == 2, "配置交接：需添加容量 rootFields"
    report = module.capacity_benchmark(settings)
    assert report["scale"]["dailyPriceRows"] == 6
    assert report["scale"]["securityRows"] == 2
    assert report["build"]["loadedPriceRows"] == 2


def test_container_proof_rejects_business_engine_before_write():
    module = capacity()
    with module.open_capacity_target() as target:
        unrelated = create_engine("postgresql+psycopg://u:secret-canary@business/db")
        try:
            with pytest.raises(module.CapacityError, match="unsafe_capacity_target"):
                target.assert_engine(unrelated)
        finally:
            unrelated.dispose()


def test_status_without_screener_skips_api_and_preserves_actual_counts():
    module = capacity()
    with module.open_capacity_target() as target:
        engine = module._engine(target.url, readonly=False, timeout=30)
        try:
            module._migrate(engine, target)
            from app.db.models import PublishedSnapshot
            with engine.begin() as connection:
                connection.execute(PublishedSnapshot.__table__.insert(), {
                    "product": "macro-insights", "version": "controlled", "as_of": datetime.now(timezone.utc), "payload": {}, "stale": False,
                })
            report = module.capacity_status(SimpleNamespace(database_read_url=target.url), api_samples=1)
            assert report["source"] == "business_readonly"
            assert report["apiStatus"] == "not_published"
            assert report["api"] == []
            assert report["database"]["counts"]["core.daily_price"] == 0
            assert report["database"]["counts"]["mart.published_snapshot"] == 1
            evidence = report["readonlyEvidence"]
            assert evidence["unchanged"] is True
            assert evidence["before"]["fingerprintSha256"] == evidence["after"]["fingerprintSha256"]
            assert evidence["before"]["rowCounts"]["core.daily_price"] == 0
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM mart.published_snapshot")).scalar_one() == 1
        finally:
            engine.dispose()


def test_expired_reader_budget_does_not_start_another_query():
    module = capacity()
    with module.open_capacity_target() as target:
        reader = module._engine(target.url, readonly=True, timeout=0.01)
        try:
            time.sleep(0.02)
            with pytest.raises(module.CapacityError, match="capacity_timeout"):
                with reader.connect() as connection:
                    connection.execute(text("SELECT 1"))
        finally:
            reader.dispose()


def test_invalid_real_builder_payload_is_blocked_before_publish(monkeypatch):
    module = capacity()
    from app.stocks.marts.stocks import ScreenerMartBuilder
    from app.marts.publication import SnapshotPublisher
    original = ScreenerMartBuilder.build
    def malformed(self, *args, **kwargs):
        candidate = original(self, *args, **kwargs)
        stocks = [{key: value for key, value in row.items() if key != "name"} for row in candidate.payload["stocks"]]
        return replace(candidate, payload={**candidate.payload, "stocks": stocks})
    def forbidden(*args, **kwargs):
        raise AssertionError("无效 DTO 已进入发布，校验错误被忽略")
    monkeypatch.setattr(ScreenerMartBuilder, "build", malformed)
    monkeypatch.setattr(SnapshotPublisher, "publish_many", forbidden)
    with pytest.raises(module.CapacityError, match="capacity_invalid_screener_payload"):
        module.capacity_benchmark(SimpleNamespace(), options=module.CapacityOptions(stock_count=2, trading_days=3, batch_size=2, api_samples=1))
