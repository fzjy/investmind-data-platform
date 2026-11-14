"""登记隔离PG17：旧/目录/核查三入口共享额度，核查与旧入口争最后槽。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import select,text
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.db.session import create_engine_and_session
from app.macro.jobs.fund_contract_runtime import run_fund_contracts
from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
from app.macro.fund_contract_discovery import build_discovery_plan
from app.macro.fund_contract_store import FundContractStore
from app.shared.readonly_database import open_readonly_factory
from backend.tests.macro.test_fund_contract_discovery import NOW,value,body
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_official_member_store import plan as legacy_plan,store as legacy_store
from backend.tests.macro.test_catalog_member_view import source_bytes
from backend.tests.macro.official_xls_fixture import compound_workbook

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_pg_shared_budget_readonly_and_last_slot_race(tmp_path,monkeypatch):
    import app.macro.jobs.fund_contract_runtime as module
    v=value();v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True);v['officialEvidence']['maxHttpPerRun']=1
    catalog=load(tmp_path,v);monkeypatch.setattr(module,'load_enterprise_target_catalog',lambda:catalog)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head');engine,factory=create_engine_and_session(url)
        try:
            settings=Settings.from_env();preview=run_fund_contracts(settings,dry_run=True,clock=lambda:NOW)
            assert preview['readOnly'] and preview['supplierHttp']==0 and count(factory,JobRun)==0
            with open_readonly_factory(url) as readonly:
                with readonly.begin() as session:
                    with pytest.raises(Exception):session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('forbidden','forbidden','{}')"))
            old=legacy_store(factory);p=legacy_plan(NOW-timedelta(days=1));key=old.reserve(p,p['policy']['documents'][0],NOW)
            old.receive(key,compound_workbook(),NOW);old.replay(key,NOW)
            catalog_store=CatalogMemberStore(factory);p=build_catalog_member_plan(catalog,'000905.SH',NOW)
            key=catalog_store.reserve(p,p['policy']['documents'][0],NOW);catalog_store.receive(key,source_bytes(),NOW);catalog_store.replay(key,NOW)
            calls=[]
            def handle(request):
                with factory() as session:
                    assert session.scalar(select(JobRun).where(JobRun.business_key=='fund-contract:512660')).request_count==1
                calls.append(str(request.url));return response(200,content=body())
            result=run_fund_contracts(settings,clock=lambda:NOW+timedelta(seconds=2),transport=httpx.MockTransport(handle))
            assert result['supplierHttp']==1 and not result['proofReady'] and count(factory,SourcePayload)==3
            assert [g['status'] for g in result['groups']]==['completed','budget_pending']
            at=NOW+timedelta(seconds=5);new=build_discovery_plan(catalog,'fund-515220',at);old_plan=legacy_plan(at);barrier=Barrier(2)
            def reserve(which):
                barrier.wait(timeout=5)
                try:
                    if which==0:return FundContractStore(factory).reserve(new,at)
                    return old.reserve(old_plan,old_plan['policy']['documents'][0],at)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(reserve,[0,1]))
            assert sum(r is not None for r in results)==1 and count(factory,JobRun)==4
            from app.macro.official_member_budget import daily_budget
            with factory() as session:assert daily_budget(session,at,4)['remaining']==0
        finally:engine.dispose()
