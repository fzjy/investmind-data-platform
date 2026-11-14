"""只允许登记的一次性PG；不连接业务库、不执行降级。"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='disposable PG tests require explicit runner flag')]


def test_0012_upgrade_preserves_existing_records(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[3]
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.chdir(root)
        monkeypatch.setattr(config_module, 'PROJECT_ENV_FILE', tmp_path / 'absent.env')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        config = Config(str(root / 'backend/alembic.ini'))
        command.upgrade(config, '20261014_0011')
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('stock-frozen','legacy',CAST(:proof AS json))"), {'proof': '{"proof":true}'})
            before = inspect(engine).get_table_names(schema='core')
            command.upgrade(config, '20261015_0012')
            command.upgrade(config, '20261015_0012')
            assert set(inspect(engine).get_table_names(schema='core')) - set(before) == {'market_daily_quote'}
            with engine.connect() as conn:
                assert conn.execute(text("SELECT cursor_value FROM ops.job_cursor WHERE job_name='stock-frozen'")).scalar() == {'proof': True}
                assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar() == '20261015_0012'
            shape = inspect(engine)
            unique = shape.get_unique_constraints('market_daily_quote', schema='core')
            assert any(set(v['column_names']) == {'provider','asset_type','asset_code','trade_date'} for v in unique)
            fk = shape.get_foreign_keys('market_daily_quote', schema='core')
            assert any(v['referred_schema'] == 'raw' and v['referred_table'] == 'source_payload' for v in fk)
        finally:
            engine.dispose()
