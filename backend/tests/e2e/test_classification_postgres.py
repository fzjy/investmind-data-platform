"""一次性PG迁移/锁验收；由隔离入口登记，绝不使用业务数据库。"""
from datetime import date, datetime, timezone
import json
import os

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import httpx
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

from app.config import PROJECT_ROOT
from app.stocks.classification.repository import ClassificationRepository
from app.stocks.classification.sync import ClassificationSync, RequestBudget
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.contracts import TushareResponse
from app.db.models import Classification, ClassificationMember, PublishedSnapshot, Security, SourcePayload
from app.jobs.locks import PostgresAdvisoryJobLock
from app.stocks.universes import UniverseResolver
from backend.tests.classification.test_sync import policy
from backend.tests.classification.test_contracts import catalog_rows

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
    reason="requires registered disposable PostgreSQL17")]


@pytest.fixture(scope="module")
def pg():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres, pytest.MonkeyPatch.context() as env:
        url = postgres.get_connection_url(driver="psycopg")
        env.setenv("DANG_DATABASE_URL", url)
        env.setenv("DANG_DATABASE_READ_URL", url)
        config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
        engine = create_engine(url)
        try:
            command.upgrade(config, "20260929_0010")
            with engine.begin() as connection:
                # 0001 uses live ORM metadata; explicitly restore the historical shape on this empty disposable DB.
                connection.execute(text("ALTER TABLE core.classification DROP CONSTRAINT uq_classification_provider_source_id"))
                connection.execute(text("ALTER TABLE core.classification DROP COLUMN source"))
                connection.execute(text("ALTER TABLE core.classification ADD CONSTRAINT uq_classification_provider_id UNIQUE(provider,classification_id)"))
                connection.execute(text("ALTER TABLE core.classification_member DROP COLUMN data"))
                connection.execute(text("INSERT INTO core.security (ts_code,symbol,name,is_st,data,source) VALUES ('600519.SH','600519','sample',false,'{}','tushare')"))
                connection.execute(text("INSERT INTO core.classification (id,provider,classification_id,name,level,data) VALUES (1001,'tushare','801010.SI','old',1,'{\"src\":\"SW2014\"}'),(1002,'tushare','unknown','unknown',1,'{}')"))
                connection.execute(text("INSERT INTO core.classification_member (classification_id,ts_code,effective_from) VALUES (1001,'600519.SH','2020-01-01')"))
            command.upgrade(config, "20261014_0011")
            yield engine, sessionmaker(engine, expire_on_commit=False), config
        finally:
            engine.dispose()


def test_migration_preserves_original_fields_and_member_foreign_key(pg):
    engine, factory, _ = pg
    with factory() as session:
        old = session.get(Classification, 1001)
        assert (old.classification_id, old.name, old.source, old.data) == ("801010.SI", "old", "SW2014", {"src": "SW2014"})
        assert session.get(Classification, 1002).source == "legacy_unknown"
        member = session.scalar(select(ClassificationMember))
        assert member.classification_id == 1001 and member.effective_from == date(2020, 1, 1)
        assert member.data == {}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261014_0011"


def test_same_code_new_version_and_downgrade_refusal_are_transactional(pg):
    engine, factory, config = pg
    with factory.begin() as session:
        session.add(Classification(provider="tushare", source="SW2021", classification_id="801010.SI", name="new", data={"src": "SW2021"}))
    with pytest.raises(RuntimeError, match="multiple sources"):
        command.downgrade(config, "20260929_0010")
    with factory() as session:
        assert len(session.scalars(select(Classification).where(Classification.classification_id == "801010.SI")).all()) == 2
        assert session.get(Classification, 1001).name == "old"
    assert "source" in {column["name"] for column in inspect(engine).get_columns("classification", schema="core")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261014_0011"


def test_source_lock_excludes_competing_dates_and_releases(pg):
    engine, _, _ = pg
    first, second = PostgresAdvisoryJobLock(engine), PostgresAdvisoryJobLock(engine)
    key = "sync-classification:tushare:SW2021"
    assert first.try_acquire(key)
    try:
        assert not second.try_acquire(key)
        assert second.try_acquire("sync-classification:tushare:SW2014")
        second.release("sync-classification:tushare:SW2014")
    finally:
        first.release(key)
    assert second.try_acquire(key)
    second.release(key)


def test_fresh_database_upgrade_head_handles_live_initial_metadata():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres, pytest.MonkeyPatch.context() as env:
        url = postgres.get_connection_url(driver="psycopg")
        env.setenv("DANG_DATABASE_URL", url)
        env.setenv("DANG_DATABASE_READ_URL", url)
        engine = create_engine(url)
        try:
            config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
            command.upgrade(config, "head")
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
        finally:
            engine.dispose()


def test_pg_raw_progress_rollback_keeps_unit_pending(pg):
    _, factory, _ = pg
    repository = ClassificationRepository(factory)
    identity = "c" * 64
    fields = tuple(catalog_rows()[0])
    response = TushareResponse(fields=fields, items=(tuple(catalog_rows()[0].values()),))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(SourcePayload))
    def fail(session):
        raise RuntimeError("simulated commit failure")
    event.listen(factory, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            repository.record_response(identity, "catalog:L1", response, {"src": "SW2021", "level": "L1"},
                fields, page_size=2000, observed_at=datetime.now(timezone.utc))
    finally:
        event.remove(factory, "before_commit", fail)
    assert repository.read_state(identity) == {}
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == before


def test_pg_bounded_resume_preserves_published_snapshot(pg):
    engine, factory, _ = pg
    selection = UniverseResolver().resolve("representative-50")
    with factory.begin() as session:
        for symbol in selection.symbols:
            if symbol != "600519.SH":
                session.add(Security(ts_code=symbol, symbol=symbol[:6], name="sample"))
        session.add(PublishedSnapshot(product="screener", version="old-publication",
            as_of=datetime(2026, 9, 18, tzinfo=timezone.utc), payload={"stocks": [{"symbol": "600519.SH", "industry": ""}]}))
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        fields = body["fields"].split(",")
        rows = ([row for row in catalog_rows() if row["level"] == body["params"]["level"]]
            if body["api_name"] == "index_classify" else [])
        return httpx.Response(200, json={"code": 0, "data": {"fields": fields,
            "items": [[row.get(field) for field in fields] for row in rows]}})
    for limit, want_requests, want_status in [(4, 4, "partial"), (128, 99, "completed"), (128, 0, "completed")]:
        budget = RequestBudget(limit)
        with TushareClient("test-token", transport=httpx.MockTransport(handler), before_request=budget.consume) as client:
            result = ClassificationSync(factory, client, budget, selection, policy(), PostgresAdvisoryJobLock(engine)).run()
        assert result["status"] == want_status and result["requests"] == want_requests
    assert len(calls) == 103
    with factory() as session:
        pointer = session.get(PublishedSnapshot, "screener")
        assert pointer.version == "old-publication"
        assert pointer.payload == {"stocks": [{"symbol": "600519.SH", "industry": ""}]}
        assert session.get(Classification, 1001).name == "old"
