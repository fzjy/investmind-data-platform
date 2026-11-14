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
from app.db.models.raw import SourcePayload
from app.db.session import create_engine_and_session
from app.macro.jobs.official_member_runtime import run_official_members
from app.macro.official_member_store import OfficialMemberStore,build_plan
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_official_member_runtime import setup
from backend.tests.macro.test_official_member_store import NOW
from backend.tests.macro.official_xls_fixture import compound_workbook

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_pg_dry_run_raw_replay_and_one_winning_concurrent_reservation(tmp_path,monkeypatch):
    _,catalog=setup(tmp_path,monkeypatch)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            settings=Settings.from_env()
            preview=run_official_members(settings,dry_run=True,clock=lambda:NOW)
            assert preview['status']=='preview' and preview['readOnly'] and preview['supplierHttp']==0
            assert count(factory,JobRun)==0
            calls=[]
            def handler(request):
                assert count(factory,JobRun)==1 and count(factory,SourcePayload)==0
                calls.append(request);return response(200,content=compound_workbook())
            first=run_official_members(settings,transport=httpx.MockTransport(handler),clock=lambda:NOW)
            assert first['status']=='completed' and first['supplierHttp']==1 and count(factory,SourcePayload)==1
            again=run_official_members(settings,transport=httpx.MockTransport(handler),clock=lambda:NOW)
            assert again['supplierHttp']==0 and again['readOnly'] and len(calls)==1
            tomorrow=NOW+timedelta(days=1);plan=build_plan(catalog.official_policy,catalog.fingerprint,tomorrow)
            barrier=Barrier(2)
            def reserve():
                barrier.wait(timeout=5)
                try:return OfficialMemberStore(factory).reserve(plan,plan['policy']['documents'][0],tomorrow)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures=[executor.submit(reserve) for _ in range(2)]
                assert sum(f.result(timeout=15) is not None for f in futures)==1
            assert count(factory,JobRun)==2
        finally:engine.dispose()
