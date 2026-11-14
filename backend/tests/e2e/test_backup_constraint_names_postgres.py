"""真实迁移命名约定的精确兼容；所有DDL仅限登记一次性PG。"""
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import PROJECT_ROOT, Settings
from app.ops.backup.config import backup_options, parse_target
from app.ops.backup.contracts import BackupError
from app.ops.backup.inventory import assert_restored_inventory, read_inventory, snapshot_inventory
from app.ops.backup.service import open_source_engine
from app.ops.backup.tools import open_tools
from backend.tests.e2e.controlled_backup import disposable_restore_target
from backend.tests.ops.backup.test_constraint_compatibility import CONVENTION_CASES

pytestmark = [pytest.mark.postgres,pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS")!="1",reason="registered disposable PG17 only")]


def test_convention_named_checks_real_roundtrip_preserves_names_and_rejects_changed_values(tmp_path):
    with PostgresContainer("postgres:17-alpine",driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        previous = vars(config_module).get("settings")
        config_module.settings = Settings(_env_file=None,database_url=url)
        try: command.upgrade(Config(str(PROJECT_ROOT/"backend/alembic.ini")),"head")
        finally:
            if previous is None: del config_module.settings
            else: config_module.settings=previous
        source = create_engine(url)
        options = backup_options(Settings(_env_file=None))
        try:
            with source.begin() as c:
                for schema,table,old,new,_,_ in CONVENTION_CASES:
                    c.execute(text(f'ALTER TABLE "{schema}"."{table}" RENAME CONSTRAINT "{old}" TO "{new}"'))
                c.execute(text("INSERT INTO ops.enterprise_receivable_attempt(id,run_id,request_key,plan_fingerprint,request,page_size,state) VALUES ('attempt','run','key','plan','{}',1,'received')"))
            with snapshot_inventory(source,timeout=120) as view, open_tools(options) as tools:
                expected = read_inventory(view.connection,timeout=120)
                archive = tmp_path/"convention-names.dump"
                tools.dump(parse_target(url),view.snapshot_id,archive,120)
                with disposable_restore_target(options) as target:
                    tools.restore(target.target,archive,120)
                    restored = open_source_engine(target.target)
                    try:
                        with snapshot_inventory(restored,timeout=120) as recovered_view:
                            actual = read_inventory(recovered_view.connection,timeout=120)
                            assert actual != expected  # PG反解析的cast分配真实不同。
                            assert_restored_inventory(recovered_view.connection,actual,expected,timeout=120)
                            for schema,table,_,name,_,_ in CONVENTION_CASES:
                                assert recovered_view.connection.scalar(text("SELECT count(*) FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND conname=:name"),{"table":schema+"."+table,"name":name})==1
                        # 不让同名但改枚举的约束通过；仅修改自有恢复目标。
                        with restored.connect() as c:
                            c.execute(text("SET default_transaction_read_only=off")); c.commit()
                            with c.begin():
                                c.execute(text("ALTER TABLE ops.enterprise_receivable_attempt DROP CONSTRAINT ck_enterprise_receivable_attempt_receivable_attempt_state"))
                                c.execute(text("ALTER TABLE ops.enterprise_receivable_attempt ADD CONSTRAINT ck_enterprise_receivable_attempt_receivable_attempt_state CHECK (state IN ('reserved','received','failed','unexpected'))"))
                        with snapshot_inventory(restored,timeout=120) as recovered_view:
                            actual = read_inventory(recovered_view.connection,timeout=120)
                            with pytest.raises(BackupError) as caught:
                                assert_restored_inventory(recovered_view.connection,actual,expected,timeout=120)
                            assert caught.value.reason_code=="restored_inventory_mismatch"
                    finally: restored.dispose()
            with snapshot_inventory(source,timeout=120) as view:
                assert read_inventory(view.connection,timeout=120)==expected
        finally: source.dispose()
