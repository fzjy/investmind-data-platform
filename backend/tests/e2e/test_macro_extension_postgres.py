"""登记一次性PG17：真实锁、事务、Raw重放与同目标幂等。"""
from datetime import timedelta
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from alembic import command
from alembic.config import Config
import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.db.session import create_engine_and_session
from app.jobs.locks import PostgresAdvisoryJobLock
from app.macro.jobs.macro_extension_runtime import LOCK_KEY, run_macro_extensions
from app.macro.extensions.store import ExtensionStore
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_runtime import enabled
from backend.tests.macro.test_extension_store import NOW, count
from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG17 only')]
ROOT = Path(__file__).resolve().parents[3]


def test_pg_independent_groups_lock_and_zero_http_reentry(tmp_path, monkeypatch):
    catalog = enabled(tmp_path, monkeypatch)
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        monkeypatch.setenv('DANG_TUSHARE_TOKEN', 'controlled-private')
        command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        try:
            settings = Settings.from_env()
            lock = PostgresAdvisoryJobLock(engine)
            assert lock.try_acquire(LOCK_KEY)
            try:
                assert run_macro_extensions(settings, now=NOW)['status'] == 'locked'
            finally:
                lock.release(LOCK_KEY)
            seen = []
            def handler(request):
                body = json.loads(request.content)
                group = 'usdcnh' if body['api_name'] == 'fx_daily' else 'nominalGdp'
                with ThreadPoolExecutor(max_workers=1) as executor:
                    state = executor.submit(lambda: ExtensionStore(factory).inspect(
                        build_extension_plan(group, catalog, NOW), NOW)).result()
                assert state['status'] == 'uncertain'
                seen.append(body['api_name'])
                return response(200, content=json.dumps({'code': 0, 'data': fx() if group == 'usdcnh' else gdp()}).encode())
            first = run_macro_extensions(settings, transport=httpx.MockTransport(handler), now=NOW)
            assert first['status'] == 'completed' and first['supplierHttp'] == 2
            assert count(factory, MacroObservation) == 5 and count(factory, JobRun) == 2
            again = run_macro_extensions(settings, transport=httpx.MockTransport(handler), now=NOW + timedelta(seconds=1))
            assert again['status'] == 'completed' and again['supplierHttp'] == 0
            assert seen == ['fx_daily', 'cn_gdp'] and count(factory, MacroObservation) == 5
            # 同一组已有根时真正竞争预留：必须仅一事务成功，另一方不获发送资格。
            barrier = Barrier(2)
            tomorrow = NOW + timedelta(days=1)
            next_plan = build_extension_plan('usdcnh', catalog, tomorrow)
            def competing_reservation():
                barrier.wait(timeout=5)
                try:
                    return ExtensionStore(factory).reserve(next_plan, tomorrow)
                except ExtensionError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(competing_reservation) for _ in range(2)]
                outcomes = [future.result(timeout=10) for future in futures]
            assert sum(outcome is not None for outcome in outcomes) == 1
            assert count(factory, JobRun) == 3 and count(factory, MacroObservation) == 5
        finally:
            engine.dispose()


def test_pg_recovery_commit_visibility_parent_lock_and_immutable_failure(tmp_path, monkeypatch):
    from app.macro.jobs.macro_extension_recovery import run_macro_extension_recovery
    from app.macro.extensions.recovery import RecoveryStore
    from backend.tests.macro.test_extension_recovery import row_value
    from sqlalchemy import select
    from app.db.models.ops import JobCursor
    catalog = enabled(tmp_path, monkeypatch)
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        monkeypatch.setenv('DANG_TUSHARE_TOKEN', 'controlled-private')
        command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        try:
            parents, plans = [], []
            for group in ('usdcnh', 'nominalGdp'):
                p = build_extension_plan(group, catalog, NOW)
                key = ExtensionStore(factory).reserve(p, NOW)
                ExtensionStore(factory).fail(key, 'EXTENSION_RESPONSE_INVALID', NOW + timedelta(seconds=1))
                plans.append(p)
                parents.append(key)
            with factory() as session:
                old_jobs = [row_value(session.get(JobRun, key)) for key in parents]
                old_cursors = [row_value(row) for row in session.scalars(select(JobCursor).order_by(JobCursor.id))]
            lock = PostgresAdvisoryJobLock(engine)
            assert lock.try_acquire(LOCK_KEY)
            try:
                assert run_macro_extension_recovery(Settings.from_env(), parents, now=NOW + timedelta(seconds=2))['status'] == 'locked'
            finally:
                lock.release(LOCK_KEY)
            calls = []
            def handler(request):
                api = json.loads(request.content)['api_name']
                index = 0 if api == 'fx_daily' else 1
                with ThreadPoolExecutor(max_workers=1) as executor:
                    state = executor.submit(lambda: RecoveryStore(factory, parents[index]).inspect(plans[index], NOW + timedelta(seconds=10))).result()
                assert state['status'] == 'uncertain'
                calls.append(api)
                return response(200, content=json.dumps({'code': 0, 'data': fx() if index == 0 else gdp()}).encode())
            result = run_macro_extension_recovery(Settings.from_env(), parents, transport=httpx.MockTransport(handler), now=NOW + timedelta(seconds=10))
            assert result['status'] == 'completed' and result['supplierHttp'] == 2
            repeated = run_macro_extension_recovery(Settings.from_env(), parents, transport=httpx.MockTransport(handler), now=NOW + timedelta(days=2))
            assert repeated['supplierHttp'] == 0 and calls == ['fx_daily', 'cn_gdp']
            assert count(factory, MacroObservation) == 5
            with factory() as session:
                assert [row_value(session.get(JobRun, key)) for key in parents] == old_jobs
                assert [row_value(row) for row in session.scalars(select(JobCursor).order_by(JobCursor.id))] == old_cursors
            later = NOW + timedelta(days=1)
            next_plan = build_extension_plan('usdcnh', catalog, later)
            next_parent = ExtensionStore(factory).reserve(next_plan, later)
            ExtensionStore(factory).fail(next_parent, 'EXTENSION_RESPONSE_INVALID', later + timedelta(seconds=1))
            barrier = Barrier(2)
            def compete():
                barrier.wait(timeout=5)
                try:
                    return RecoveryStore(factory, next_parent).reserve(next_plan, later + timedelta(seconds=2))
                except ExtensionError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                pending = [executor.submit(compete) for _ in range(2)]
                results = [future.result(timeout=15) for future in pending]
            assert sum(value is not None for value in results) == 1
            assert count(factory, JobRun) == 6 and count(factory, MacroObservation) == 5
        finally:
            engine.dispose()


def test_pg_vix_explicit_group_uses_fred_us_core_and_zero_http_reentry(tmp_path, monkeypatch):
    from backend.tests.macro.test_extension_vix import VIX_NOW, vix_config
    from backend.tests.macro.test_extension_vix_runtime import fred_payload
    from app.macro.extensions import config as configuration
    vix_config(tmp_path)
    monkeypatch.setattr(configuration, 'DEFAULT_EXTENSION_CONFIG_FILE', tmp_path / 'extensions.json')
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        monkeypatch.setenv('DANG_TUSHARE_TOKEN', 'tushare-private')
        monkeypatch.setenv('DANG_FRED_API_KEY', 'fred-private-key')
        command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        calls = []
        try:
            def handler(request):
                calls.append(request)
                assert request.url.params['api_key'] == 'fred-private-key'
                assert 'tushare-private' not in str(request.url)
                assert count(factory, JobRun) == 1
                return response(200, content=json.dumps(fred_payload()).encode())
            settings = Settings.from_env()
            first = run_macro_extensions(settings, group_ids=['vix'], transport=httpx.MockTransport(handler), now=VIX_NOW)
            assert first['status'] == 'completed' and first['supplierHttp'] == 1 and len(first['groups']) == 1
            second = run_macro_extensions(settings, group_ids=['vix'], transport=httpx.MockTransport(handler), now=VIX_NOW + timedelta(seconds=1))
            assert second['supplierHttp'] == 0 and len(calls) == 1 and count(factory, MacroObservation) == 2
            with factory() as session:
                from sqlalchemy import select
                assert all(row.region == 'US' and row.unit == 'index' and row.source == 'fred' for row in session.scalars(select(MacroObservation)))
        finally:
            engine.dispose()
