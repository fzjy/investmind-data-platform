"""登记PG17：统一入口、持久预留、原件重放、真实唯一键竞争。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_extension_runtime import run_macro_extensions
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import build_extension_plan
from app.macro.fiscal_store import FiscalStore
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_fiscal_execution import setup, body
from backend.tests.macro.test_fiscal_plan import NOW

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG17 only')]
ROOT = Path(__file__).resolve().parents[3]


def test_pg_official_fiscal_committed_raw_and_single_winning_reservation(tmp_path, monkeypatch):
    c = setup(tmp_path, monkeypatch, lookback=3)
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database'); monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        try:
            calls = []
            def handler(request):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    state = executor.submit(lambda: FiscalStore(factory).inspect(build_extension_plan('governmentBonds', c, NOW), NOW)).result()
                assert state['status'] == 'blocked' and state['reservedCount'] == 1
                calls.append(request); return response(200, content=body(2026))
            settings = Settings.from_env()
            first = run_macro_extensions(settings, group_ids=['governmentBonds'], now=NOW, transport=httpx.MockTransport(handler))
            assert first['status'] == 'completed' and first['supplierHttp'] == 1
            assert count(factory, MacroObservation) == 3
            again = run_macro_extensions(settings, group_ids=['governmentBonds'], now=NOW, transport=httpx.MockTransport(handler))
            assert again['supplierHttp'] == 0 and len(calls) == 1
            p = build_extension_plan('governmentBonds', c, NOW + timedelta(days=8))
            barrier = Barrier(2)
            def reserve():
                barrier.wait(timeout=5)
                try:
                    return FiscalStore(factory).reserve(p, p['documents'][0], NOW + timedelta(days=8))
                except ExtensionError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [executor.submit(reserve) for _ in range(2)]
                assert sum(f.result(timeout=10) is not None for f in results) == 1
            assert count(factory, JobRun) == 2
        finally:
            engine.dispose()


def test_pg_raw_replay_is_atomic_concurrent_and_preserves_failed_parent(tmp_path,monkeypatch):
    from app.macro.jobs.macro_fiscal_runtime import run_fiscal_raw_replay, OfficialFiscalClient
    from backend.tests.macro.test_fiscal_raw_replay import failed_raw, original_rows
    c=setup(tmp_path,monkeypatch,lookback=3)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as postgres:
        url=postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database'); monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            key=failed_raw(factory,c); before=original_rows(factory,key)
            monkeypatch.setattr(OfficialFiscalClient,'fetch',lambda *_:pytest.fail('raw replay must not send'))
            settings=Settings.from_env()
            from app.shared.readonly_database import open_readonly_factory
            with open_readonly_factory(url) as readonly_factory:
                assert FiscalStore(readonly_factory).replay_failed_raw(key,dry_run=True)['status']=='preview'
            preview=run_fiscal_raw_replay(settings,key,dry_run=True)
            assert preview['status']=='preview' and preview['readOnly'] and preview['supplierHttp']==0
            assert count(factory,MacroObservation)==0
            barrier=Barrier(2)
            def replay():
                barrier.wait(timeout=5)
                return FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=1))
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures=[executor.submit(replay) for _ in range(2)]
                reports=[f.result(timeout=15) for f in futures]
            assert sorted(r['insertedRows'] for r in reports)==[0,3]
            assert all(r['supplierHttp']==0 for r in reports)
            assert count(factory,MacroObservation)==3 and original_rows(factory,key)==before
            again=run_fiscal_raw_replay(settings,key)
            assert again['status']=='completed' and again['supplierHttp']==0 and again['readOnly']
        finally: engine.dispose()
