import os
import pytest
from sqlalchemy import create_engine
from testcontainers.community.postgres import PostgresContainer

from app.local_admin.tables import cell, list_tables, preview

pytestmark = pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="disposable PostgreSQL")


def test_browse_real_records():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url())
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA core")
            conn.exec_driver_sql("CREATE SCHEMA raw")
            conn.exec_driver_sql("CREATE TABLE raw.sample (value text)")
            conn.exec_driver_sql("CREATE TABLE core.sample (day int, id int, value jsonb, PRIMARY KEY(day,id))")
            conn.exec_driver_sql("INSERT INTO core.sample SELECT 1, n, to_jsonb(repeat('x',20000)) FROM generate_series(1,30) n")
        assert len(list_tables(engine)) == 2
        page = preview(engine, "core", "sample")
        assert len(page["rows"]) == 25 and page["nextCursor"] == 25
        assert page["rows"][0]["more"][2]
        assert cell(engine, "core", "sample", "value", page["rows"][0]["id"], 0)["next"] == 8192
        assert len(preview(engine, "core", "sample", 25)["rows"]) == 5
        assert preview(engine, "raw", "sample")["rows"] == []
        with pytest.raises(KeyError):
            preview(engine, "pg_catalog", "pg_authid")
        engine.dispose()
