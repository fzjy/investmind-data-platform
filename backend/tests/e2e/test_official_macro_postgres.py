"""登记隔离 PG17：正常入口、真正只读预检及唯一预留竞争。"""
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
from app.db.models.ops import JobRun
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_extension_runtime import run_macro_extensions
from app.macro.extensions.plan import build_extension_plan
from app.macro.official_macro_store import OfficialMacroStore
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_official_macro_runtime import setup
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_property_loans import directory

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_pg_readonly_preview_raw_core_reentry_and_concurrent_reservation(monkeypatch):
    _,config=setup(monkeypatch)
    import app.macro.jobs.macro_extension_runtime as runtime
    import app.macro.extensions.preview as preview
    monkeypatch.setattr(runtime,'load_extension_config',lambda:config)
    monkeypatch.setattr(preview,'load_extension_config',lambda:config)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            settings=Settings.from_env()
            before=run_macro_extensions(settings,dry_run=True,group_ids=['householdCredit'],now=NOW)
            assert before['readOnly'] and before['supplierHttp']==0 and count(factory,JobRun)==0
            calls=[]
            def handler(request):
                calls.append(request)
                assert count(factory,JobRun)==len(calls)
                return response(200,content=document(title='2026年前八个月金融统计数据报告') if len(calls)==1 else document())
            first=run_macro_extensions(settings,group_ids=['householdCredit'],now=NOW,transport=httpx.MockTransport(handler))
            assert first['status']=='completed' and first['supplierHttp']==2
            again=run_macro_extensions(settings,group_ids=['householdCredit'],now=NOW,transport=httpx.MockTransport(handler))
            assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==2
            after=run_macro_extensions(settings,dry_run=True,group_ids=['householdCredit'],now=NOW)
            assert after['readOnly'] and after['groups'][0]['inventory']['validVisibleRows']==2
            later=NOW+timedelta(days=8)
            p=build_extension_plan('householdCredit',config,later)
            barrier=Barrier(2)
            def reserve():
                barrier.wait(timeout=5)
                try:return OfficialMacroStore(factory).reserve(p,p['documents'][0],later)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures=[executor.submit(reserve) for _ in range(2)]
                assert sum(f.result(timeout=15) is not None for f in futures)==1
            assert count(factory,JobRun)==3
        finally:engine.dispose()


def test_registered_pg_no_candidate_is_partial_and_next_week_checks_directory(monkeypatch):
    _,config=setup(monkeypatch,'propertyLoans')
    import app.macro.jobs.macro_extension_runtime as runtime
    monkeypatch.setattr(runtime,'load_extension_config',lambda:config)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            calls=[]
            def handler(request):calls.append(request);return response(200,content=directory([]))
            settings=Settings.from_env()
            first=run_macro_extensions(settings,group_ids=['propertyLoans'],now=NOW,transport=httpx.MockTransport(handler))
            again=run_macro_extensions(settings,group_ids=['propertyLoans'],now=NOW,transport=httpx.MockTransport(handler))
            later=run_macro_extensions(settings,group_ids=['propertyLoans'],now=NOW+timedelta(days=8),transport=httpx.MockTransport(handler))
            assert first['status']==again['status']==later['status']=='partial'
            assert first['supplierHttp']==later['supplierHttp']==1 and again['supplierHttp']==0 and again['readOnly']
            assert len(calls)==2 and count(factory,JobRun)==2
        finally:engine.dispose()
