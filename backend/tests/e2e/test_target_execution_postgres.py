"""0017、PG原子预留和真实dump/restore；仅登记一次性容器。"""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer

from app.config import PROJECT_ROOT,Settings
from app.db.session import create_engine_and_session
from app.ops.backup.config import backup_options,parse_target
from app.ops.backup.inventory import snapshot_inventory,read_inventory,assert_restored_inventory
from app.ops.backup.tools import open_tools
from app.ops.backup.service import open_source_engine
from backend.tests.e2e.controlled_backup import disposable_restore_target
from backend.tests.macro.test_target_execution_store import NOW,RUN,OTHER,metadata_response
from backend.tests.macro.test_target_execution_plan import configured

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]


def test_0017_install_protection_and_real_archive_roundtrip(monkeypatch,tmp_path):
    mod=import_module('app.macro.target_execution_store')
    gate=import_module('app.macro.target_execution_schema').target_execution_schema
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg');monkeypatch.setenv('DANG_DATABASE_URL',url);monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        config=Config(str(PROJECT_ROOT/'backend/alembic.ini'));command.upgrade(config,'20261022_0016')
        engine,factory=create_engine_and_session(url)
        try:
            assert not inspect(engine).has_table('enterprise_target_execution_plan',schema='ops')
            with snapshot_inventory(engine,timeout=120) as view: before=read_inventory(view.connection,timeout=120)
            command.upgrade(config,'20261022_0017');command.upgrade(config,'20261022_0017')
            assert gate(factory)==()
            from app.db.models.enterprise_target_execution import TargetExecutionPlan
            from sqlalchemy.orm import sessionmaker
            with pytest.raises(DBAPIError),engine.begin() as connection:
                connection.execute(TargetExecutionPlan.__table__.insert().values(
                    slot='second',fingerprint='a'*64,snapshot={},snapshot_hash='a'*64))
            catalog,selection=configured(tmp_path,codes=('000300.SH',))
            check_name=inspect(engine).get_check_constraints('enterprise_target_execution_attempt',schema='ops')[0]['name']
            for literal in ('re served','RESERVED','res(erved)'):
                with engine.connect() as connection:
                    transaction=connection.begin()
                    try:
                        name=connection.dialect.identifier_preparer.quote(check_name)
                        connection.execute(text(f'ALTER TABLE ops.enterprise_target_execution_attempt DROP CONSTRAINT {name}'))
                        connection.execute(text(f"ALTER TABLE ops.enterprise_target_execution_attempt ADD CONSTRAINT {name} CHECK (state = '{literal}' OR state = 'received' OR state = 'failed')"))
                        controlled=sessionmaker(bind=connection)
                        assert 'ops.enterprise_target_execution_attempt:check' in gate(controlled)
                        with pytest.raises(ValueError,match='TARGET_EXECUTION_SCHEMA_REQUIRED'):
                            mod.TargetExecutionStore(controlled).prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True)
                        with controlled() as session:assert session.scalars(select(TargetExecutionPlan)).all()==[]
                    finally:transaction.rollback()
            with snapshot_inventory(engine,timeout=120) as view: after=read_inventory(view.connection,timeout=120)
            old={(t.schema_name,t.table_name):t for t in before.tables if t.table_name!='alembic_version'}
            assert all(t==old[(t.schema_name,t.table_name)] for t in after.tables if (t.schema_name,t.table_name) in old)
            store=mod.TargetExecutionStore(factory);root=store.prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True)
            step=store.progress(root)['next'];key=store.reserve(root,step,RUN,NOW)
            store.receive(key,metadata_response(),NOW,catalog.evidence_policy);store.progress(root,persist=True)
            for sql in ('UPDATE ops.enterprise_target_execution_plan SET fingerprint=fingerprint','DELETE FROM ops.enterprise_target_execution_plan'):
                with pytest.raises(DBAPIError,match='TARGET_EXECUTION_IMMUTABLE'),engine.begin() as connection:connection.execute(text(sql))
            with engine.connect() as connection:
                transaction=connection.begin()
                try:
                    connection.execute(text('ALTER TABLE ops.enterprise_target_execution_plan DISABLE TRIGGER immutable_enterprise_target_execution'))
                    from sqlalchemy.orm import Session
                    assert gate(lambda:Session(bind=connection))
                finally:transaction.rollback()
            options=backup_options(Settings(_env_file=None))
            with snapshot_inventory(engine,timeout=120) as view,open_tools(options) as tools:
                expected=read_inventory(view.connection,timeout=120);archive=tmp_path/'execution.dump'
                tools.dump(parse_target(url),view.snapshot_id,archive,120)
                with disposable_restore_target(options) as target:
                    tools.restore(target.target,archive,120);restored=open_source_engine(target.target)
                    try:
                        with snapshot_inventory(restored,timeout=120) as recovered:
                            actual=read_inventory(recovered.connection,timeout=120)
                            assert_restored_inventory(recovered.connection,actual,expected,timeout=120)
                    finally:restored.dispose()
            with pytest.raises(RuntimeError,match='DOWNGRADE_DISABLED'):command.downgrade(config,'20261022_0016')
        finally:engine.dispose()


def test_concurrent_reservation_cannot_spend_last_root_allowance_twice(monkeypatch,tmp_path):
    mod=import_module('app.macro.target_execution_store')
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg');monkeypatch.setenv('DANG_DATABASE_URL',url)
        command.upgrade(Config(str(PROJECT_ROOT/'backend/alembic.ini')),'20261022_0017')
        engine,factory=create_engine_and_session(url)
        try:
            from dataclasses import replace
            catalog,selection=configured(tmp_path,codes=('000300.SH',))
            catalog=replace(catalog,evidence_policy=replace(catalog.evidence_policy,max_http_per_run=1,max_http_per_plan=1))
            store=mod.TargetExecutionStore(factory);root=store.prepare(catalog,selection,NOW.date(),'Asia/Shanghai',persist=True);step=store.progress(root)['next']
            def reserve(run):
                try:mod.TargetExecutionStore(factory).reserve(root,step,run,NOW);return True
                except ValueError:return False
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[RUN,OTHER]))
            assert sorted(results)==[False,True]
            assert store.progress(root)['rootReservedCount']==1
        finally:engine.dispose()


def test_runtime_pg_schema_lock_token_recapture_and_zero_http_reentry(monkeypatch,tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    from app.macro.jobs import target_execution_runtime as runtime
    from app.jobs.locks import job_lock_for_engine
    from app.macro import enterprise_target_catalog
    from app.db.models.enterprise_target_execution import TargetExecutionPlan
    from backend.tests.macro.test_target_execution_batch import provider
    catalog,selection=configured(tmp_path,codes=('000300.SH',))
    catalog=replace(catalog,evidence_policy=replace(catalog.evidence_policy,collection_enabled=True))
    monkeypatch.setattr(enterprise_target_catalog,'load_enterprise_target_catalog',lambda:catalog)
    monkeypatch.setattr(runtime,'resolve_enterprise_selection',lambda *_:selection)
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg');monkeypatch.setenv('DANG_DATABASE_URL',url)
        config=Config(str(PROJECT_ROOT/'backend/alembic.ini'));command.upgrade(config,'20261022_0016')
        settings=Settings(_env_file=None,data_backend='database',database_url=url,database_read_url=url,tushare_token='controlled-token')
        calls=[];transport=provider(calls)
        missing=runtime.run_target_execution(settings,transport=transport,now=NOW)
        assert missing['errorCode']=='TARGET_EXECUTION_SCHEMA_REQUIRED' and missing['readOnly'] and not calls
        command.upgrade(config,'20261022_0017');engine,factory=create_engine_and_session(url)
        try:
            empty=settings.model_copy(update={'tushare_token':None})
            no_token=runtime.run_target_execution(empty,transport=transport,now=NOW)
            assert no_token['errorCode']=='TARGET_TUSHARE_TOKEN_REQUIRED' and not calls
            with factory() as s:assert s.scalars(select(TargetExecutionPlan)).all()==[]
            lock=job_lock_for_engine(engine);assert lock.try_acquire(runtime.LOCK_KEY)
            try:
                locked=runtime.run_target_execution(settings,transport=transport,now=NOW)
                assert locked['status']=='locked' and locked['readOnly'] and not calls
            finally:lock.release(runtime.LOCK_KEY)
            disabled=replace(catalog,evidence_policy=replace(catalog.evidence_policy,collection_enabled=False))
            catalogs=iter((catalog,disabled))
            monkeypatch.setattr(enterprise_target_catalog,'load_enterprise_target_catalog',lambda:next(catalogs))
            assert runtime.run_target_execution(settings,transport=transport,now=NOW)['status']=='disabled'
            assert not calls
            monkeypatch.setattr(enterprise_target_catalog,'load_enterprise_target_catalog',lambda:catalog)
            complete=runtime.run_target_execution(settings,transport=transport,now=NOW)
            assert complete['processingCompleted'] and complete['physicalBudgetEnforced']
            assert complete['requestCount']==2 and len(calls)==2 and not complete['membershipReady']
            again=runtime.run_target_execution(empty,transport=transport,now=NOW+timedelta(days=1))
            assert again['processingCompleted'] and again['requestCount']==0 and len(calls)==2
            assert again['frozenAsOfDate']=='2026-10-22'
        finally:engine.dispose()
