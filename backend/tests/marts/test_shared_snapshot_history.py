"""五种当前产品复用不可变历史，对照 PostgreSQL 的实际迁移、约束与回滚。"""

from datetime import datetime, timedelta, timezone
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

from app import config as settings_module
from app.config import PROJECT_ROOT, Settings
from app.db.models import PublishedSnapshot
from app.marts.contracts import MartCandidate
from app.marts.publication import SHARED_HISTORY_PRODUCTS, SnapshotPublisher
from app.shared.repository import SnapshotRepository

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="isolated PG17 only"
    ),
]


def test_share_release_and_rollback(monkeypatch):
    day = datetime(2026, 11, 15, tzinfo=timezone.utc)
    payload = {"label": "previous", "$investmindHistory": None}
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url(driver="psycopg")
        monkeypatch.setattr(
            settings_module, "settings", Settings(database_url=url), raising=False
        )
        cfg = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
        command.upgrade(cfg, "20261111_0020")
        engine = create_engine(url)
        try:
            factory = sessionmaker(engine, expire_on_commit=False)
            publisher = SnapshotPublisher(factory)
            repository = SnapshotRepository(factory)

            def read_inline():
                with engine.connect() as conn:
                    return dict(
                        conn.execute(
                            text("SELECT product, payload FROM mart.published_snapshot")
                        ).all()
                    )

            for name in sorted(
                SHARED_HISTORY_PRODUCTS | {"market-explorer", "stock-workbench:sample"}
            ):
                publisher.publish_many([MartCandidate(name, day, payload)], "v1", ())
            assert all(value == payload for value in read_inline().values())
            command.upgrade(cfg, "head")
            inline = read_inline()
            assert {
                k for k, v in inline.items() if isinstance(v,str)
            } == SHARED_HISTORY_PRODUCTS
            assert (
                inline["market-explorer"] == inline["stock-workbench:sample"] == payload
            )
            query_log = []

            @event.listens_for(engine, "before_cursor_execute")
            def remember(conn, cursor, statement, parameters, context, executemany):
                query_log.append(statement)

            for name in SHARED_HISTORY_PRODUCTS:
                query_log.clear()
                assert repository.read(name).raw == payload
                assert len(query_log) == 1
                assert repository.read_versioned(name, "v1").raw == payload
                query_log.clear()
                assert repository.read_meta(name).version == "v1"
                assert len(query_log) == 1 and ".payload" not in query_log[0]

            fresh = {"label": "next"}
            outcome = publisher.publish_many(
                [MartCandidate("portfolio", day, fresh)], "v2", ()
            )
            assert outcome[0].payload == fresh
            assert repository.read_versioned("portfolio", "v1").raw == payload
            assert isinstance(read_inline()["portfolio"],str)
            publisher.publish_many(
                [
                    MartCandidate(
                        "portfolio", day - timedelta(days=1), {"label": "late"}
                    )
                ],
                "older",
                (),
            )
            assert repository.read("portfolio").raw == fresh

            with pytest.raises(ValueError, match="内容不一致"):
                publisher.publish_many(
                    [
                        MartCandidate("screener", day, {"conflict": 1}),
                        MartCandidate("portfolio", day, {"conflict": 2}),
                    ],
                    "v2",
                    (),
                )
            assert repository.read_meta("screener").version == "v1"
            assert repository.read("portfolio").raw == fresh
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "DELETE FROM mart.page_snapshot WHERE product='portfolio' AND snapshot_version='v2'"
                    )

            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("""UPDATE mart.page_snapshot SET payload='{"accidental":"edit"}'
                         WHERE product='portfolio' AND snapshot_version='v2'""")
            assert repository.read("portfolio").raw == fresh
            events = {"marketNews": [{"id": "content-owned"}]}
            assert (
                publisher.publish_many(
                    [MartCandidate("portfolio", day, events)], "events", ()
                )[0].payload
                == events
            )
            assert read_inline()["portfolio"] == events
            with factory.begin() as session:
                session.delete(session.get(PublishedSnapshot, "screener"))
            assert repository.read_versioned("screener", "v1").raw == payload
            command.downgrade(cfg, "20261111_0020")
            assert all(isinstance(value,dict) for value in read_inline().values())
            assert repository.read("portfolio").raw == events
            assert repository.read_versioned("portfolio", "v2").raw == fresh
        finally:
            engine.dispose()


def test_backup_inventory_without_freshness_columns():
    from app.ops.backup.inventory import read_inventory
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(driver="psycopg"))
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("""CREATE SCHEMA mart;
                    CREATE TABLE mart.page_snapshot(id int primary key, payload json);
                    CREATE TABLE mart.published_snapshot(product text primary key, version text, stale boolean, payload json);
                    INSERT INTO mart.published_snapshot VALUES('screener','v1',false,'{"stocks":[{},{}]}')""")
                inventory = read_inventory(conn, timeout=30)
                assert [(p.product,p.row_count) for p in inventory.publications] == [("screener",2)]
        finally:
            engine.dispose()
