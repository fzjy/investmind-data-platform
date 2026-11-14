"""REA执行器正式迁移与恢复：仅登记的临时PostgreSQL17。"""
from importlib import import_module
from pathlib import Path
import os

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from testcontainers.community.postgres import PostgresContainer

from app.db.session import create_engine_and_session
from app.macro.jobs.rea_inputs_runtime import rea_input_schema
from app.macro.jobs.receivables_runtime import _schema
from app.macro.balance_supplement import ORDERS
from backend.tests.macro.test_rea_inputs_batch import execute

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG17 only')]
ROOT = Path(__file__).resolve().parents[3]


def test_rea_pg_additive_migration_resume_immutability_and_existing_products(monkeypatch):
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        config = Config(str(ROOT / 'backend/alembic.ini'))
        command.upgrade(config, '20261020_0014')
        engine, factory = create_engine_and_session(url)
        try:
            with engine.begin() as connection:
                assert int(connection.scalar(text('SHOW server_version_num'))) // 10000 == 17
                connection.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) "
                    "VALUES ('rea-guard','frozen',CAST(:p AS JSON))"), {'p': '{"keep":true}'})
            assert not inspect(engine).has_table('enterprise_rea_plan', schema='ops')
            command.upgrade(config, '20261021_0015')
            command.upgrade(config, '20261021_0015')
            assert not rea_input_schema(factory)
            assert not _schema(factory) and not _schema(factory, target=ORDERS)
            calls = []
            assert execute(factory, calls=calls)['requestCount'] == 1
            engine.dispose()
            engine, factory = create_engine_and_session(url)
            for _ in range(5):
                assert execute(factory, calls=calls)['requestCount'] == 1
            final = execute(factory, calls=calls)
            assert final['processingCompleted'] and final['requestCount'] == 0 and len(calls) == 6
            # 正式不可变PG证据：只读事务消费，不改旧结果/不写Raw重放；按真实收据日期可见。
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from app.shared.readonly_database import open_readonly_factory
            from app.macro.enterprise_rea_inputs import load_enterprise_rea_inputs
            from app.macro.enterprise_contract import public_enterprise_analysis
            from backend.tests.macro.test_rea_inputs_batch import AS_OF, POLICY, data
            from backend.tests.macro.test_enterprise_rea import metric
            today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
            with open_readonly_factory(url) as readonly:
                loaded = load_enterprise_rea_inputs(readonly, data(1), today, POLICY,
                    page_size=5000, timezone='Asia/Shanghai')
                assert len(loaded.rea_fields) == 20
                before = public_enterprise_analysis(data(1), today, POLICY, rea_enabled=True)
                after = public_enterprise_analysis(loaded, today, POLICY, rea_enabled=True)
                assert {k: v for k, v in before.items() if k != 'rea'} == {k: v for k, v in after.items() if k != 'rea'}
                p = next(p for p in metric(after['rea'], 'capacity')['points'] if p['period'] == '2024-06-30')
                assert p['amounts'] == {'a': 400, 'b': 9, 'oldA': 200, 'oldB': 9}
                historical = public_enterprise_analysis(loaded, AS_OF, POLICY, rea_enabled=True)
                assert metric(historical['rea'], 'capacity')['points'] == []
                from app.macro.enterprise_contract import valid_enterprise_analysis
                industries = public_enterprise_analysis(loaded, today, POLICY, rea_enabled=True, industry_enabled=True)
                assert valid_enterprise_analysis(industries)
                group = industries['industryComparison']['groups'][0]
                assert group['analysis']['symbols'] == list(loaded.symbols)
                assert group['name'] == '801120.SI' and group['labelSource'] == 'code-fallback'
                assert group['analysis']['policy']['min_samples'] == POLICY.min_samples
            assert execute(factory, calls=calls)['requestCount'] == 0 and len(calls) == 6
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT cursor_value FROM ops.job_cursor WHERE job_name='rea-guard'")) == {'keep': True}
                for name in ('receivable', 'order'):
                    for schema, suffix in (('ops', 'attempt'), ('raw', 'response'), ('core', 'supplement')):
                        assert connection.scalar(text(f'SELECT count(*) FROM {schema}.enterprise_{name}_{suffix}')) == 0
            for table in ('ops.enterprise_rea_plan', 'raw.enterprise_rea_response', 'core.enterprise_rea_supplement'):
                for statement in (f'UPDATE {table} SET created_at=created_at' if table != 'raw.enterprise_rea_response'
                                  else f'UPDATE {table} SET captured_at=captured_at', f'DELETE FROM {table}'):
                    with pytest.raises(DBAPIError, match='ENTERPRISE_REA_IMMUTABLE'), engine.begin() as connection:
                        connection.execute(text(statement))
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text('INSERT INTO ops.enterprise_rea_attempt SELECT * FROM ops.enterprise_rea_attempt LIMIT 1'))
            with pytest.raises(RuntimeError, match='TABLE_ALREADY_EXISTS'), engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    import_module('migrations.versions.20261021_0015_enterprise_rea_inputs').upgrade()
            with pytest.raises(RuntimeError, match='DOWNGRADE_DISABLED'):
                command.downgrade(config, '20261020_0014')

            # Each damaging DDL is rolled back; all checks exercise the real PG catalog.
            damage = [
                'ALTER TABLE raw.enterprise_rea_response DISABLE TRIGGER immutable_enterprise_rea',
                'DROP TRIGGER immutable_enterprise_rea ON raw.enterprise_rea_response',
                "CREATE OR REPLACE FUNCTION ops.reject_enterprise_rea_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$",
                'ALTER TABLE raw.enterprise_rea_response ALTER COLUMN response_hash DROP NOT NULL',
                ('DROP TRIGGER immutable_enterprise_rea ON raw.enterprise_rea_response',
                 'CREATE TRIGGER immutable_enterprise_rea BEFORE UPDATE OR DELETE ON raw.enterprise_rea_response '
                 'FOR EACH ROW WHEN (false) EXECUTE FUNCTION ops.reject_enterprise_rea_mutation()'),
            ]
            with engine.connect() as connection:
                names = connection.execute(text("SELECT contype,conname FROM pg_constraint "
                    "WHERE conrelid='core.enterprise_rea_supplement'::regclass AND contype IN ('p','f','c')")).all()
                for _, name in names:
                    damage.append('ALTER TABLE core.enterprise_rea_supplement DROP CONSTRAINT '
                        + connection.dialect.identifier_preparer.quote(name))
                name=inspect(connection).get_unique_constraints('enterprise_rea_plan',schema='ops')[0]['name']
                damage.append('ALTER TABLE ops.enterprise_rea_plan DROP CONSTRAINT '
                    + connection.dialect.identifier_preparer.quote(name))
            for statement in damage:
                connection = engine.connect()
                transaction = connection.begin()
                try:
                    for sql in (statement,) if isinstance(statement,str) else statement:
                        connection.execute(text(sql))
                    # Bind inspection to this transaction so altered catalog is visible without committing.
                    from sqlalchemy.orm import Session
                    assert rea_input_schema(lambda: Session(bind=connection)), statement
                    assert not _schema(lambda: Session(bind=connection)), 'old AR unaffected'
                finally:
                    transaction.rollback()
                    connection.close()
            assert not rea_input_schema(factory)
        finally:
            engine.dispose()
