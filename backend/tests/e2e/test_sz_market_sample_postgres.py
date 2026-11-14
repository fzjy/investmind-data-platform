"""仅登记隔离PG17：固定全项目槽并发唯一与Raw-only完成。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from app.db.session import create_engine_and_session
from app.macro.sz_market_sample import SzMarketSamplePolicy, build_sample_plan
from app.macro.sz_market_sample_store import SzMarketSampleStore
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_sz_market_sample import parent, policy, sample, execute, NOW

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
    reason='registered disposable PG17 only')]


def test_registered_pg_single_lifetime_slot_under_concurrency(monkeypatch):
    root = Path(__file__).resolve().parents[3]
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        engine, factory = create_engine_and_session(url)
        try:
            command.upgrade(Config(str(root / 'backend/alembic.ini')), 'head')
            value = policy(parent(factory))
            at = NOW + timedelta(minutes=1)
            plan = build_sample_plan(factory, SzMarketSamplePolicy.model_validate(value), at)
            barrier = Barrier(2)
            def reserve(_):
                barrier.wait(timeout=5)
                try:
                    return SzMarketSampleStore(factory).reserve(plan, at)
                except ValueError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                winners = list(executor.map(reserve, range(2)))
            assert sum(v is not None for v in winners) == 1
            SzMarketSampleStore(factory).finish(plan, at + timedelta(seconds=1), data=sample())
            assert count(factory, MacroObservation) == 1 and count(factory, JobRun) == 3
            again = execute(factory, value, lambda _: pytest.fail('pilot already consumed'), clock=lambda: at + timedelta(days=7))
            assert again['status'] == 'captured' and again['supplierHttp'] == 0
        finally:
            engine.dispose()
