"""只在隔离入口登记的PG17验证0016，不迁移业务库。"""
from importlib import import_module
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from app.db.session import create_engine_and_session
from backend.tests.macro.test_target_evidence_normalize import request, response, CAPTURED, AS_OF
from backend.tests.macro.test_target_evidence_policy import policy

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
    reason='registered disposable PG17 only')]
ROOT = Path(__file__).resolve().parents[3]


def test_target_pg_install_upgrade_guards_and_catalog_damage(monkeypatch):
    try:
        mod = import_module('app.macro.target_evidence_store')
        gate = import_module('app.macro.target_evidence_schema').target_evidence_schema
    except ImportError:
        pytest.fail('target evidence schema/store/migration not implemented')
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        config = Config(str(ROOT / 'backend/alembic.ini'))
        command.upgrade(config, '20261021_0015')
        engine, factory = create_engine_and_session(url)
        try:
            assert not inspect(engine).has_table('enterprise_target_observation', schema='raw')
            assert not inspect(engine).has_table('enterprise_target_candidate', schema='core')
            with engine.begin() as c:
                assert int(c.scalar(text('SHOW server_version_num'))) // 10000 == 17
                c.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('target-guard','frozen',CAST(:payload AS JSON))"), {'payload': '{"keep":true}'})
            # ORM负责项目声明的非server默认值，避免SQL种子漏data/is_st。
            from app.db.models import Security, PublishedSnapshot, PageSnapshot
            with factory.begin() as session:
                session.add(Security(ts_code='000001.SZ', symbol='000001', name='guard'))
                session.add(PublishedSnapshot(product='guard', version='old', as_of=CAPTURED, payload={'keep': True}))
                session.add(PageSnapshot(product='guard', snapshot_version='old', as_of=CAPTURED, payload={'keep': True}))
            def protected():
                with engine.connect() as c:
                    return (c.execute(text('SELECT * FROM ops.job_cursor ORDER BY job_name,cursor_key')).all(),
                        c.execute(text('SELECT * FROM core.security ORDER BY ts_code')).all(),
                        c.execute(text('SELECT * FROM mart.published_snapshot')).all(),
                        c.execute(text('SELECT * FROM mart.page_snapshot')).all())
            before = protected()
            command.upgrade(config, '20261022_0016')
            command.upgrade(config, '20261022_0016')
            assert protected() == before and gate(factory) == ()
            store = mod.TargetEvidenceStore(factory)
            key = store.receive(request(), response(), CAPTURED, policy())
            value = store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
            assert value['completeness'] == 'not_proven'
            assert store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True) == value
            assert store.inventory() == {'observationCount': 1, 'candidateCount': 1}
            assert protected() == before
            for table, column in [('raw.enterprise_target_observation', 'captured_at'), ('core.enterprise_target_candidate', 'created_at')]:
                for sql in (f'UPDATE {table} SET {column}={column}', f'DELETE FROM {table}'):
                    with pytest.raises(DBAPIError, match='TARGET_EVIDENCE_IMMUTABLE'), engine.begin() as c:
                        c.execute(text(sql))
            damage = [
                ('DROP TRIGGER immutable_enterprise_target ON raw.enterprise_target_observation',
                 'CREATE TRIGGER immutable_enterprise_target BEFORE UPDATE OF response_hash OR DELETE ON raw.enterprise_target_observation FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_target_mutation()'),
                'ALTER TABLE raw.enterprise_target_observation DISABLE TRIGGER immutable_enterprise_target',
                'DROP TRIGGER immutable_enterprise_target ON core.enterprise_target_candidate',
                "CREATE OR REPLACE FUNCTION ops.reject_enterprise_target_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$",
                'ALTER TABLE raw.enterprise_target_observation ALTER COLUMN response_hash DROP NOT NULL',
                'ALTER TABLE raw.enterprise_target_observation ADD COLUMN unexpected text',
                ('DROP TRIGGER immutable_enterprise_target ON raw.enterprise_target_observation',
                 'CREATE TRIGGER immutable_enterprise_target BEFORE UPDATE OR DELETE ON raw.enterprise_target_observation FOR EACH ROW WHEN (false) EXECUTE FUNCTION ops.reject_enterprise_target_mutation()'),
            ]
            with engine.connect() as c:
                for table in ('raw.enterprise_target_observation', 'core.enterprise_target_candidate'):
                    for name, in c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid=CAST(:t AS regclass) AND contype IN ('p','f','u','c')"), {'t': table}):
                        damage.append(f'ALTER TABLE {table} DROP CONSTRAINT ' + c.dialect.identifier_preparer.quote(name) + ' CASCADE')
            for statements in damage:
                c = engine.connect(); transaction = c.begin()
                try:
                    for sql in (statements,) if isinstance(statements, str) else statements:
                        c.execute(text(sql))
                    assert gate(lambda: Session(bind=c)), statements
                finally:
                    transaction.rollback(); c.close()
            assert gate(factory) == ()
            with pytest.raises(RuntimeError, match='DOWNGRADE_DISABLED'):
                command.downgrade(config, '20261021_0015')
        finally:
            engine.dispose()
