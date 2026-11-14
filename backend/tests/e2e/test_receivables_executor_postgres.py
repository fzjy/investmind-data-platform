"""专用补充迁移与恢复，仅登记的一次性PostgreSQL17。"""
from importlib import import_module
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect,text
from sqlalchemy.exc import DBAPIError,IntegrityError
from testcontainers.community.postgres import PostgresContainer

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from backend.tests.macro.receivables_samples import full_inputs
from backend.tests.macro.test_receivables_batch import run,transport_for

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_pg_additive_migration_immutability_unique_lock_and_durable_resume(monkeypatch):
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        config=Config(str(ROOT/'backend/alembic.ini'))
        command.upgrade(config,'20261015_0012')
        engine,factory=create_engine_and_session(url)
        try:
            with engine.begin() as connection:
                assert int(connection.scalar(text('SHOW server_version_num')))//10000==17
                connection.execute(text("INSERT INTO core.security(ts_code,symbol,name,is_st,data,source) VALUES ('000001.SZ','000001','preserved',false,'{}','controlled')"))
                connection.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('frozen','fixed',CAST(:payload AS JSON))"),{'payload':'{"keep":true}'})
            assert not inspect(engine).has_table('enterprise_receivable_response',schema='raw'), '0013 tables must not be created by old migrations'
            command.upgrade(config,'20261018_0013')
            command.upgrade(config,'20261018_0013')
            assert inspect(engine).has_table('enterprise_receivable_response',schema='raw'), 'supplement migration not implemented'
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT name FROM core.security WHERE ts_code='000001.SZ'"))=='preserved'
                assert connection.scalar(text("SELECT cursor_value FROM ops.job_cursor WHERE job_name='frozen'"))=={'keep':True}
            first_lock,other_lock=job_lock_for_engine(engine),job_lock_for_engine(engine)
            assert first_lock.try_acquire('enterprise-receivables')
            try: assert not other_lock.try_acquire('enterprise-receivables')
            finally: first_lock.release('enterprise-receivables')
            assert other_lock.try_acquire('enterprise-receivables')
            other_lock.release('enterprise-receivables')
            batch=import_module('app.macro.jobs.receivables_batch')
            data=full_inputs(); calls=[]
            assert run(batch,factory,data,transport_for(data,calls),limit=1)['pendingCount']==3
            engine.dispose();engine,factory=create_engine_and_session(url)
            assert run(batch,factory,data,transport_for(data,calls),limit=3)['matchedCount']==4
            assert run(batch,factory,data,transport_for(data,calls))['requestCount']==0 and len(calls)==4
            for table in ('raw.enterprise_receivable_response','core.enterprise_receivable_supplement'):
                for operation in ('UPDATE','DELETE'):
                    statement=f'{operation} {table} SET captured_at=captured_at' if operation=='UPDATE' and table.startswith('raw') else (
                        f'{operation} {table} SET value=value' if operation=='UPDATE' else f'DELETE FROM {table}')
                    with pytest.raises(DBAPIError,match='ENTERPRISE_RECEIVABLE_IMMUTABLE'),engine.begin() as connection:
                        connection.execute(text(statement))
            with pytest.raises(IntegrityError),engine.begin() as connection:
                connection.execute(text('INSERT INTO core.enterprise_receivable_supplement SELECT * FROM core.enterprise_receivable_supplement LIMIT 1'))
            with pytest.raises(RuntimeError,match='DOWNGRADE_DISABLED'):
                command.downgrade(config,'20261015_0012')
        finally: engine.dispose()


def test_pg_runtime_lock_refresh_schema_guard_and_completed_reentry(monkeypatch):
    from datetime import UTC,datetime
    from sqlalchemy import select
    from app.config import Settings
    from app.db.models import Security,IncomeStatement,BalanceSheet,Classification,ClassificationMember
    from app.macro.enterprise_repository import load_enterprise_inputs
    from app.stocks.universes import UniverseResolver
    from app.macro.jobs import receivables_runtime as runtime
    import httpx
    import json
    from backend.tests.macro.receivables_samples import response_for
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            data=full_inputs()
            with factory.begin() as session:
                for security in data.securities:
                    session.add(Security(ts_code=security.symbol,symbol=security.symbol[:6],name='Controlled',list_date=security.list_date))
                session.flush()
                for row in data.financials:
                    model=IncomeStatement if row.table=='income' else BalanceSheet
                    session.add(model(ts_code=row.symbol,end_date=row.end_date,ann_date=row.ann_date,f_ann_date=row.f_ann_date,
                        report_type=row.report_type,comp_type=row.comp_type,update_flag=row.update_flag,data=dict(row.values)))
                root=Classification(provider='tushare',source='SW2021',classification_id='801120.SI',name='root',level=1)
                middle=Classification(provider='tushare',source='SW2021',classification_id='801121.SI',name='middle',level=2,parent_id='801120.SI')
                leaf=Classification(provider='tushare',source='SW2021',classification_id='851121.SI',name='leaf',level=3,parent_id='801121.SI')
                session.add_all((root,middle,leaf));session.flush()
                for security in data.securities:
                    session.add(ClassificationMember(ts_code=security.symbol,classification_id=leaf.id,effective_from=security.list_date))
            settings=Settings(_env_file=None,data_backend='database',database_url=url,
                macro_enterprise_min_coverage=1,macro_enterprise_min_samples=1,macro_catchup_request_limit=100,
                tushare_token='controlled-token',tushare_standard_requests_per_minute=1000000)
            now=datetime(2024,5,31,10,tzinfo=UTC);calls=[]
            def handle(request):
                params=json.loads(request.content)['params'];calls.append(params)
                current=load_enterprise_inputs(factory,UniverseResolver().resolve('representative-50'))
                source=next(row for row in current.financials if row.table=='balance' and row.symbol==params['ts_code'] and row.end_date.strftime('%Y%m%d')==params['period'])
                return httpx.Response(200,json={'code':0,'data':response_for(source).model_dump(mode='json')})
            transport=httpx.MockTransport(handle)
            holder=job_lock_for_engine(engine)
            assert holder.try_acquire(runtime.LOCK_KEY)
            try:
                locked=runtime.run_enterprise_receivables(settings,transport=transport,now=now)
                assert locked['status']=='locked' and locked['requestCount']==0 and not calls
            finally: holder.release(runtime.LOCK_KEY)
            original=runtime.job_lock_for_engine
            class RefreshLock:
                def __init__(self,engine): self.inner=original(engine)
                def try_acquire(self,key):
                    acquired=self.inner.try_acquire(key)
                    if acquired:
                        with factory.begin() as session:
                            row=session.scalar(select(BalanceSheet).order_by(BalanceSheet.ts_code,BalanceSheet.end_date))
                            row.data={**row.data,'money_cap':999}
                    return acquired
                def release(self,key): self.inner.release(key)
            monkeypatch.setattr(runtime,'job_lock_for_engine',RefreshLock)
            first=runtime.run_enterprise_receivables(settings,transport=transport,now=now,probe=True)
            assert (first['status'],first['requestCount'],first['matchedCount'],first['unavailableCount'])==('partial',1,1,0)
            assert first['httpLimit']==1 and first['configuredHttpLimit']==100 and first['probeReady'] is True
            monkeypatch.setattr(runtime,'job_lock_for_engine',original)
            second=runtime.run_enterprise_receivables(settings,transport=transport,now=now)
            # 受控000002不在实际固定池，runtime不得为了凑4请求扩大池。
            assert second['matchedCount']==2 and second['requestCount']==1
            assert second['planFingerprint']==first['planFingerprint']
            third=runtime.run_enterprise_receivables(settings,transport=transport,now=now)
            assert third['status']=='completed' and third['requestCount']==0 and len(calls)==2
            assert all(params['ts_code']=='000001.SZ' for params in calls)
            cached_probe=runtime.run_enterprise_receivables(settings,transport=transport,now=now,probe=True)
            assert cached_probe['requestCount']==0 and cached_probe['probeReady'] is False and len(calls)==2
            policy_block=runtime.run_enterprise_receivables(settings.model_copy(update={'tushare_page_size':1}),transport=transport,now=now)
            assert policy_block['status']=='blocked' and policy_block['errorCode']=='ENTERPRISE_PAGE_POLICY_CHANGED' and len(calls)==2
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE raw.enterprise_receivable_response DISABLE TRIGGER immutable_enterprise_receivable'))
            blocked=runtime.run_enterprise_receivables(settings,transport=transport,now=now)
            assert blocked['status']=='blocked' and blocked['errorCode']=='ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED' and len(calls)==2
        finally: engine.dispose()
