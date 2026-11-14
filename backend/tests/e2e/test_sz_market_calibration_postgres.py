"""登记隔离PG17验证零HTTP派生的事务锁和单一Core。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.db.session import create_engine_and_session
from app.macro.sz_market_calibration import CalibrationStore
from backend.tests.macro.test_sz_market_calibration import captured

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
    reason='registered disposable PG17 only')]


def test_zero_http_calibration_is_atomic_under_concurrency(monkeypatch):
    root = Path(__file__).resolve().parents[3]
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        engine, factory = create_engine_and_session(url)
        try:
            command.upgrade(Config(str(root / 'backend/alembic.ini')), 'head')
            at = captured(factory) + timedelta(seconds=1)
            barrier = Barrier(2)
            def replay(index):
                barrier.wait(timeout=10)
                return CalibrationStore(factory).replay(at+timedelta(microseconds=index), clock=lambda:datetime.now(timezone.utc))
            with ThreadPoolExecutor(max_workers=2) as executor: results = list(executor.map(replay, range(2)))
            assert sorted(r['insertedRows'] for r in results) == [0, 1]
            assert all(r['supplierHttp'] == 0 for r in results)
            proof = CalibrationStore(factory).load(datetime.now(timezone.utc))
            assert proof['point']['value'] == '331511.2085844745'
            again = CalibrationStore(factory).replay(at-timedelta(microseconds=1), clock=lambda:datetime.now(timezone.utc))
            assert again['readOnly'] is True and again['insertedRows'] == 0
            assert CalibrationStore(factory).load(at) is None
        finally: engine.dispose()
