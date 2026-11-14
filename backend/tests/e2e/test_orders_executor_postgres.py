"""订单独立迁移与不可变持久恢复：仅登记的临时PostgreSQL17。"""
from pathlib import Path
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect,text
from sqlalchemy.exc import DBAPIError,IntegrityError
from testcontainers.community.postgres import PostgresContainer

from app.db.session import create_engine_and_session
from app.macro.jobs.receivables_runtime import _schema
from app.macro.balance_supplement import ORDERS
from backend.tests.macro.receivables_samples import full_inputs
from backend.tests.macro.test_enterprise_orders_supplement import execute

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_order_pg_additive_migration_guard_resume_unique_and_old_ar_unchanged(monkeypatch):
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        config=Config(str(ROOT/'backend/alembic.ini'))
        command.upgrade(config,'20261018_0013')
        engine,factory=create_engine_and_session(url)
        try:
            with engine.begin() as connection:
                assert int(connection.scalar(text('SHOW server_version_num')))//10000==17
                connection.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('frozen','fixed',CAST(:p AS JSON))"),{'p':'{"keep":true}'})
            assert not inspect(engine).has_table('enterprise_order_response',schema='raw')
            command.upgrade(config,'20261020_0014');command.upgrade(config,'20261020_0014')
            assert _schema(factory,target=ORDERS)==[] and _schema(factory)==[]
            checks={schema+'.'+name:inspect(engine).get_check_constraints(name,schema=schema) for schema,name in (
                ('ops','enterprise_order_attempt'),('core','enterprise_order_supplement'))}
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT cursor_value FROM ops.job_cursor WHERE job_name='frozen'"))=={'keep':True}
            data=full_inputs();calls=[]
            assert execute(factory,data,calls,limit=1,probe=True)['probeReady']
            engine.dispose();engine,factory=create_engine_and_session(url)
            assert execute(factory,data,calls,limit=3)['matchedCount']==4
            assert execute(factory,data,calls)['requestCount']==0 and len(calls)==4
            with engine.connect() as connection:
                for table in ('ops.enterprise_receivable_attempt','raw.enterprise_receivable_response','core.enterprise_receivable_supplement'):
                    assert connection.scalar(text(f'SELECT count(*) FROM {table}'))==0
            for table,column in (('raw.enterprise_order_response','captured_at'),('core.enterprise_order_supplement','value')):
                for sql in (f'UPDATE {table} SET {column}={column}',f'DELETE FROM {table}'):
                    with pytest.raises(DBAPIError,match='ENTERPRISE_ORDER_IMMUTABLE'),engine.begin() as connection:
                        connection.execute(text(sql))
            with pytest.raises(IntegrityError),engine.begin() as connection:
                connection.execute(text('INSERT INTO core.enterprise_order_supplement SELECT * FROM core.enterprise_order_supplement LIMIT 1'))
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE raw.enterprise_order_response DISABLE TRIGGER immutable_enterprise_order'))
            assert 'raw.enterprise_order_response:immutability-guard' in _schema(factory,target=ORDERS)
            with engine.begin() as connection:
                connection.execute(text('DROP TRIGGER immutable_enterprise_order ON raw.enterprise_order_response'))
                connection.execute(text('CREATE TRIGGER immutable_enterprise_order BEFORE UPDATE OR DELETE ON raw.enterprise_order_response '
                    'FOR EACH ROW WHEN (false) EXECUTE FUNCTION ops.reject_enterprise_order_mutation()'))
            assert 'raw.enterprise_order_response:immutability-guard' in _schema(factory,target=ORDERS),checks
            with pytest.raises(RuntimeError,match='TABLE_ALREADY_EXISTS'):
                from importlib import import_module
                from alembic.migration import MigrationContext
                from alembic.operations import Operations
                with engine.begin() as connection:
                    with Operations.context(MigrationContext.configure(connection)):
                        import_module('migrations.versions.20261020_0014_enterprise_orders').upgrade()
            with pytest.raises(RuntimeError,match='DOWNGRADE_DISABLED'):
                command.downgrade(config,'20261018_0013')
        finally: engine.dispose()


@pytest.mark.parametrize('damage',['function-body','primary-key','foreign-key','check','nullable'])
def test_order_pg_rejects_schema_that_has_names_but_not_required_protection(monkeypatch,damage):
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            assert not _schema(factory,target=ORDERS)
            with engine.begin() as c:
                if damage=='function-body': c.execute(text('CREATE OR REPLACE FUNCTION ops.reject_enterprise_order_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$'))
                elif damage=='nullable': c.execute(text('ALTER TABLE raw.enterprise_order_response ALTER COLUMN response_hash DROP NOT NULL'))
                else:
                    kind={'primary-key':'p','foreign-key':'f','check':'c'}[damage]
                    name=c.scalar(text("SELECT conname FROM pg_constraint WHERE conrelid='core.enterprise_order_supplement'::regclass AND contype=:kind ORDER BY conname LIMIT 1"),{'kind':kind})
                    c.execute(text('ALTER TABLE core.enterprise_order_supplement DROP CONSTRAINT '+c.dialect.identifier_preparer.quote(name)))
                    if damage=='check': c.execute(text('ALTER TABLE core.enterprise_order_supplement ADD CONSTRAINT '+c.dialect.identifier_preparer.quote(name)+' CHECK (true)'))
            assert _schema(factory,target=ORDERS),damage
            assert not _schema(factory),'old AR schema unaffected by order damage'
        finally: engine.dispose()
