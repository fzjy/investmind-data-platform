"""登记隔离 PG17；不使用业务库，验证统一入口、只读和持久并发。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date,timedelta
from decimal import Decimal
import json
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
from app.db.models.core import MacroObservation,TradeCalendar
from app.db.models.ops import JobCursor,JobRun
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_extension_runtime import run_macro_extensions
from app.macro.exchange_summary_store import ExchangeSummaryStore,plan_from_database
from app.shared.readonly_database import open_readonly_factory
from backend.tests.macro.test_extension_store import count
from backend.tests.macro.test_exchange_summary_plan import calendar
from backend.tests.macro.test_exchange_summary_store import NOW
from backend.tests.macro.test_exchange_summary_client import response,TOKEN

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_registered_pg_normal_preview_precise_core_and_single_concurrent_reservation(monkeypatch):
    import app.macro.jobs.macro_extension_runtime as runtime
    import app.macro.extensions.preview as preview
    import app.macro.jobs.exchange_summary_runtime as exchange
    from app.macro.extensions.config import ExtensionConfig,load_extension_config
    from backend.tests.macro.test_exchange_summary_config import policy
    data=load_extension_config().model_dump()
    for key in ('marginBalances','aShareFloatMv'):
        data['groups'][key]=policy(key);data['groups'][key]['collectionEnabled']=True
    config=ExtensionConfig.model_validate(data)
    for module in (runtime,preview,exchange):monkeypatch.setattr(module,'load_extension_config',lambda:config)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND','database');monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url);monkeypatch.setenv('DANG_TUSHARE_TOKEN',TOKEN)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            with factory.begin() as session:
                session.add_all([TradeCalendar(exchange=r['exchange'],cal_date=date.fromisoformat(r['date']),is_open=r['open'],
                    pretrade_date=date.fromisoformat(r['previous'])) for r in calendar()])
            settings=Settings.from_env();groups=['marginBalances','aShareFloatMv']
            before=run_macro_extensions(settings,dry_run=True,group_ids=groups,now=NOW)
            assert before['readOnly'] and before['supplierHttp']==0 and count(factory,JobRun)==0
            with open_readonly_factory(url) as readonly:
                with readonly.begin() as session:
                    with pytest.raises(Exception):session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('forbidden','forbidden','{}')"))
            calls=[]
            def handler(request):
                q=json.loads(request.content);calls.append(q)
                assert count(factory,JobRun)==len(calls)
                fields=q['fields'].split(',')
                row=[q['params'].get(f,'123456789.1234567890') for f in fields]
                return response(value={'code':0,'data':{'fields':fields,'items':[row]}})
            first=run_macro_extensions(settings,group_ids=groups,now=NOW,transport=httpx.MockTransport(handler))
            assert first['status']=='completed' and first['supplierHttp']==5 and count(factory,MacroObservation)==5
            with factory() as session:
                assert all(r.value==Decimal('123456789.1234567890') for r in session.scalars(select(MacroObservation)))
            again=run_macro_extensions(settings,group_ids=groups,now=NOW+timedelta(hours=10),transport=httpx.MockTransport(handler))
            assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==5
            after=run_macro_extensions(settings,dry_run=True,group_ids=groups,now=NOW+timedelta(hours=10))
            assert [g['inventory']['validVisibleRows'] for g in after['groups']]==[3,2]
            later=NOW+timedelta(days=1);p=plan_from_database(factory,config,'marginBalances',later);barrier=Barrier(2)
            def reserve():
                barrier.wait(timeout=5)
                try:return ExchangeSummaryStore(factory).reserve(p,p['documents'][0],later)
                except Exception:return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(lambda _:reserve(),range(2)))
            assert sum(r is not None for r in results)==1
            assert count(factory,JobRun)==6 and count(factory,JobCursor)==3
        finally:engine.dispose()
