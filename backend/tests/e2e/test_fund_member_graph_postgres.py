"""登记隔离PG17：正常CSI入口、真实父收据及最后共享槽并发。"""
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
from app.db.session import create_engine_and_session
from app.macro.jobs.fund_member_graph_runtime import run_fund_graphs
from app.macro.fund_member_graph import build_fund_graph_intent
from app.macro.fund_member_graph_store import FundMemberGraphStore,resolve_source_contract
from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
from app.macro.enterprise_official_targets import load_official_witnesses
from app.shared.readonly_database import open_readonly_factory
from backend.tests.macro.test_fund_member_graph import NOW,value
from backend.tests.macro.test_fund_member_graph_store import setup as metadata
from backend.tests.macro.test_fund_member_graph_runtime import handler
from backend.tests.macro.test_catalog_member_view import source_bytes
from backend.tests.macro.test_official_member_store import plan as old_plan,store as old_store
from backend.tests.macro.test_official_pcf import body as pcf_body
from backend.tests.macro.test_extension_store import count

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_graph_normal_entry_proof_and_last_budget_race(tmp_path,monkeypatch):
    import app.macro.jobs.fund_member_graph_runtime as module
    v=value();v['catalogEvidence'].update(collectionEnabled=True,memberGraphEnabled=True);v['officialEvidence']['maxHttpPerRun']=1
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head');engine,factory=create_engine_and_session(url)
        try:
            c,_=metadata(factory,tmp_path,v=v);monkeypatch.setattr(module,'load_enterprise_target_catalog',lambda:c)
            settings=Settings.from_env();preview=run_fund_graphs(settings,dry_run=True,clock=lambda:NOW)
            assert preview['readOnly'] and preview['supplierHttp']==0 and count(factory,JobRun)==1
            with open_readonly_factory(url) as readonly:
                with readonly.begin() as session:
                    with pytest.raises(Exception):session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('forbidden','forbidden','{}')"))
            index=CatalogMemberStore(factory);p=build_catalog_member_plan(c,'000905.SH',NOW)
            key=index.reserve(p,p['policy']['documents'][0],NOW);index.receive(key,source_bytes(),NOW);index.replay(key,NOW)
            calls=[];out=run_fund_graphs(settings,clock=lambda:NOW+timedelta(seconds=2),transport=httpx.MockTransport(handler(factory,calls)))
            assert out['supplierHttp']==1 and len(calls)==1 and out['groups'][0]['status']=='budget_pending'
            assert count(factory,JobRun)==3
            at=NOW+timedelta(seconds=5);proof=resolve_source_contract(factory,c,'512660.SH',at);intent=build_fund_graph_intent(c,'512660.SH',proof,at)
            legacy=old_store(factory);op=old_plan(at);s=FundMemberGraphStore(factory);barrier=Barrier(2)
            def reserve(which):
                barrier.wait(timeout=5)
                try:
                    if which==0:return s.reserve(intent,'fund-pcf',at)
                    return legacy.reserve(op,op['policy']['documents'][0],at)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:result=list(executor.map(reserve,[0,1]))
            assert sum(r is not None for r in result)==1 and count(factory,JobRun)==4
            if result[0] is None:
                at=NOW+timedelta(days=1);key=s.reserve(intent,'fund-pcf',at)
            else:key=result[0]
            s.receive(key,pcf_body(code='512660',day='20261022'),at+timedelta(seconds=1));s.replay(key,at+timedelta(seconds=2))
            witnesses=load_official_witnesses(factory,at+timedelta(seconds=3),c)
            assert {w['targetCode'] for w in witnesses}=={'000905.SH','512660.SH'}
            assert s.inspect(intent,at+timedelta(seconds=3))['witnessReady']
        finally:engine.dispose()
