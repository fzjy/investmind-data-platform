"""登记临时PG17：真只读、正常入口、跨家族并发争最后额度。"""
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
from app.macro.jobs.catalog_member_runtime import run_catalog_members
from app.macro.catalog_member_store import CatalogMemberStore,build_catalog_member_plan
from app.shared.readonly_database import open_readonly_factory
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_catalog_member_policy import NOW,value,source
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_catalog_member_runtime import handler
from backend.tests.macro.test_official_member_store import plan as legacy_plan,store as legacy_store
from backend.tests.macro.official_xls_fixture import compound_workbook

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_pg_readonly_normal_source_and_cross_entry_last_daily_slot(tmp_path,monkeypatch):
    import app.macro.jobs.catalog_member_runtime as module
    v=value();v['catalogEvidence']['collectionEnabled']=True;v['officialEvidence']['maxHttpPerRun']=2
    v['targets'].append({'kind':'etf','code':'512660.SH','label':'军工ETF','sourceIndexNameHint':'中证军工指数'})
    v['catalogEvidence']['targets'].append({'code':'512660.SH','documents':[
        source('399967'),
        {'id':'fund-512660','kind':'fund-mapping','indexCode':'399967','fundCode':'512660','subscriptionCode':None,
            'tradingDate':None,'url':'https://e.gtfund.com/etrade/Jijin/view/id/512660#etf-list'},
        {'id':'pcf-512660','kind':'fund-pcf','indexCode':'399967','fundCode':'512660','subscriptionCode':'512661',
            'tradingDate':'2026-10-23','url':'https://m.gtfund.com/cochin/etf/download/512661/20261023'}]})
    catalog=load(tmp_path,v);monkeypatch.setattr(module,'load_enterprise_target_catalog',lambda:catalog)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            settings=Settings.from_env()
            preview=run_catalog_members(settings,dry_run=True,clock=lambda:NOW)
            assert preview['readOnly'] and preview['supplierHttp']==0 and count(factory,JobRun)==0
            with open_readonly_factory(url) as readonly:
                with readonly.begin() as session:
                    with pytest.raises(Exception):session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('forbidden','forbidden','{}')"))
            calls=[];first=run_catalog_members(settings,clock=lambda:NOW,transport=httpx.MockTransport(handler(factory,calls)))
            assert first['supplierHttp']==2 and count(factory,SourcePayload)==2
            assert [g['status'] for g in first['groups']]==['completed','completed','source_pending','budget_pending']
            at=NOW+timedelta(seconds=2);p=legacy_plan(NOW-timedelta(days=1));old=legacy_store(factory)
            key=old.reserve(p,p['policy']['documents'][0],at)
            old.receive(key,compound_workbook(),at+timedelta(seconds=1));old.replay(key,at+timedelta(seconds=2))
            assert count(factory,JobRun)==3
            at=NOW+timedelta(seconds=10);p1=build_catalog_member_plan(catalog,'512660.SH',at);p2=legacy_plan(at)
            barrier=Barrier(2)
            def reserve(which):
                barrier.wait(timeout=5)
                try:
                    if which==0:return CatalogMemberStore(factory).reserve(p1,p1['policy']['documents'][0],at)
                    return old.reserve(p2,p2['policy']['documents'][0],at)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(reserve,[0,1]))
            assert sum(r is not None for r in results)==1 and count(factory,JobRun)==4
            from app.macro.official_member_budget import daily_budget
            with factory() as session:assert daily_budget(session,at,4)['remaining']==0
        finally:engine.dispose()
