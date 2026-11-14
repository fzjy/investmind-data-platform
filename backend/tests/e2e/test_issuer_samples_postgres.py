"""登记隔离PG17：原件仅Raw、真只读、采样与旧入口争最后一个额度。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.db.session import create_engine_and_session
from app.macro.jobs.issuer_sample_runtime import run_issuer_samples
from app.macro.issuer_contract_samples import build_sample_plan
from app.macro.issuer_sample_store import IssuerSampleStore
from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
from app.shared.readonly_database import open_readonly_factory
from backend.tests.macro.test_issuer_contract_samples import NOW,value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_official_member_store import plan as old_plan,store as old_store
from backend.tests.macro.test_catalog_member_view import source_bytes
from backend.tests.macro.official_xls_fixture import compound_workbook

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_sample_raw_readonly_and_shared_last_slot_race(tmp_path,monkeypatch):
    import app.macro.jobs.issuer_sample_runtime as module
    v=value();v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True);v['officialEvidence']['maxHttpPerRun']=1
    c=load(tmp_path,v);monkeypatch.setattr(module,'load_enterprise_target_catalog',lambda:c)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head');engine,factory=create_engine_and_session(url)
        try:
            settings=Settings.from_env();preview=run_issuer_samples(settings,dry_run=True,clock=lambda:NOW)
            assert preview['status']=='preview' and preview['supplierHttp']==0 and count(factory,JobRun)==0
            with open_readonly_factory(url) as readonly:
                with readonly.begin() as session:
                    with pytest.raises(Exception):session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('forbidden','forbidden','{}')"))
            old=old_store(factory);p=old_plan(NOW);key=old.reserve(p,p['policy']['documents'][0],NOW)
            old.receive(key,compound_workbook(),NOW);old.replay(key,NOW)
            index=CatalogMemberStore(factory);ip=build_catalog_member_plan(c,'000905.SH',NOW)
            key=index.reserve(ip,ip['policy']['documents'][0],NOW);index.receive(key,source_bytes(),NOW);index.replay(key,NOW)
            out=run_issuer_samples(settings,clock=lambda:NOW+timedelta(seconds=2),transport=httpx.MockTransport(lambda r:response(200,content=b'opaque issuer sample')))
            assert out['supplierHttp']==1 and not out['proofReady'] and count(factory,SourcePayload)==3
            assert [g['status'] for g in out['groups']]==['sample_received','budget_pending']
            sample=IssuerSampleStore(factory);at=NOW+timedelta(seconds=5);new=build_sample_plan(c,'159928',at);barrier=Barrier(2)
            def reserve(which):
                barrier.wait(timeout=5)
                try:
                    return sample.reserve(new,at) if which==0 else old.reserve(p,p['policy']['documents'][1],at)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(reserve,[0,1]))
            assert sum(r is not None for r in results)==1 and count(factory,JobRun)==4
            from app.macro.official_member_budget import daily_budget
            with factory() as session:assert daily_budget(session,at,4)['remaining']==0
            state=sample.inspect(build_sample_plan(c,'512800',at),at)
            assert state['phase']=='received' and state['normalized'] is None and not state['proofReady']
        finally:engine.dispose()
