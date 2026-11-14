"""真实保护图/归档/恢复：只用登记的一次性PG17，绝不连接业务库。"""
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import PROJECT_ROOT, Settings
from app.ops.backup import inventory, service
from app.ops.backup.config import backup_options
from app.ops.backup.contracts import BackupError
from app.ops.backup.files import BackupStore
from app.ops.backup.tools import DockerTools
from backend.tests.e2e.controlled_backup import disposable_restore_target, seed_backup_database
from backend.tests.ops.backup.test_project_guards import GROUPS

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="registered disposable PG17 only")]


def seed_protected_rows(connection, with_target):
    for suffix in ("receivable", "order"):
        connection.execute(text(f"INSERT INTO ops.enterprise_{suffix}_attempt(id,run_id,request_key,plan_fingerprint,request,page_size,state) VALUES ('attempt','run','key','plan','{{}}',1,'received')"))
        connection.execute(text(f"INSERT INTO raw.enterprise_{suffix}_response(attempt_id,request_key,payload,response_hash) VALUES ('attempt','key','{{}}','hash')"))
        connection.execute(text(f"INSERT INTO core.enterprise_{suffix}_supplement(request_key,response_id,core_binding,witness_version,status,value) VALUES ('key','attempt','{{}}','controlled','matched','1')"))
    connection.execute(text("INSERT INTO ops.enterprise_rea_plan(fingerprint,sequence_number,snapshot,snapshot_hash,witness_version) VALUES ('plan',1,'{}','hash','controlled')"))
    connection.execute(text("INSERT INTO ops.enterprise_rea_attempt(request_key,plan_fingerprint,run_id,request,page_size,state) VALUES ('key','plan','run','{}',1,'received')"))
    connection.execute(text("INSERT INTO raw.enterprise_rea_response(request_key,payload,response_hash) VALUES ('key','{}','hash')"))
    connection.execute(text("INSERT INTO core.enterprise_rea_supplement(request_key,core_binding,witness_version,status,outcome) VALUES ('key','{}','controlled','matched','{}')"))
    if with_target:
        connection.execute(text("INSERT INTO raw.enterprise_target_observation(observation_id,provider,target_code,target_kind,api_name,params,requested_fields,request_hash,response,response_hash,captured_at) VALUES ('observation','tushare','000300.SH','index','index_weight','{}','[]','hash','{}','hash',now())"))
        connection.execute(text("INSERT INTO core.enterprise_target_candidate(candidate_id,observation_id,normalizer_version,policy_hash,as_of,timezone,payload,payload_hash) VALUES ('candidate','observation','controlled','policy',CURRENT_DATE,'Asia/Shanghai','{}','hash')"))


@pytest.fixture(scope="module", params=["20261021_0015", "20261022_0016"])
def source(request, tmp_path_factory):
    revision = request.param
    root = tmp_path_factory.mktemp("project-guard-packages")
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        previous = vars(config_module).get("settings")
        config_module.settings = Settings(_env_file=None, database_url=url)
        try:
            command.upgrade(Config(str(PROJECT_ROOT / "backend/alembic.ini")), revision)
        finally:
            if previous is None: del config_module.settings
            else: config_module.settings = previous
        engine = create_engine(url)
        try:
            seed_backup_database(sessionmaker(engine, expire_on_commit=False))
            with engine.begin() as connection: seed_protected_rows(connection, revision == "20261022_0016")
            with inventory.snapshot_inventory(engine, timeout=120) as view:
                expected = inventory.read_inventory(view.connection, timeout=120)
            settings = Settings(_env_file=None, database_url=url, backup_path=str(root))
            backup = service.create_backup(settings, force=True)
            yield settings, engine, expected, backup.backup_id, revision
        finally:
            engine.dispose()


def mutation_statements(change):
    table = "raw.enterprise_receivable_response"
    trigger = "immutable_enterprise_receivable"
    create = f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_receivable_mutation()"
    if change == "body": return ["CREATE OR REPLACE FUNCTION ops.reject_enterprise_receivable_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$"]
    if change == "disabled": return [f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"]
    if change == "columns": return [f"DROP TRIGGER {trigger} ON {table}", create.replace("UPDATE OR DELETE", "UPDATE OF payload OR DELETE")]
    if change == "when": return [f"DROP TRIGGER {trigger} ON {table}", create.replace("FOR EACH ROW EXECUTE", "FOR EACH ROW WHEN (false) EXECUTE")]
    if change == "wrong-table": return [f"DROP TRIGGER {trigger} ON {table}", create.replace(f"ON {table}", "ON core.security")]
    if change == "all-removed": return [f"DROP FUNCTION IF EXISTS ops.reject_enterprise_{suffix}_mutation() CASCADE" for suffix, _, _ in GROUPS]
    if change == "extra": return ["CREATE FUNCTION ops.unrecognized() RETURNS integer LANGUAGE sql AS 'SELECT 1'"]
    raise AssertionError(change)


DAMAGE = ("body", "disabled", "columns", "when", "wrong-table", "all-removed", "extra")


def assert_source_unchanged(engine, expected):
    with inventory.snapshot_inventory(engine, timeout=120) as view:
        actual = inventory.read_inventory(view.connection, timeout=120)
        inventory.assert_restored_inventory(view.connection, actual, expected, timeout=120)


def test_revision_guards_roundtrip_and_restored_guards_reject_mutation(source, monkeypatch):
    settings, engine, expected, identifier, revision = source
    created = []
    groups = GROUPS[:4 if revision == "20261022_0016" else 3]
    def check_rows(target):
        restored = service.open_source_engine(target)
        try:
            assert_source_unchanged(restored, expected)
            with restored.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off")); connection.commit()
                for _, marker, tables in groups:
                    for schema, table in tables:
                        count = connection.scalar(text(f'SELECT count(*) FROM "{schema}"."{table}"'))
                        column = connection.scalar(text("SELECT attname FROM pg_attribute WHERE attrelid=CAST(:name AS regclass) AND attnum=1"), {"name":f"{schema}.{table}"})
                        connection.rollback()
                        assert count > 0
                        for sql in (f'UPDATE "{schema}"."{table}" SET "{column}"="{column}"', f'DELETE FROM "{schema}"."{table}"'):
                            with pytest.raises(DBAPIError, match=marker):
                                with connection.begin(): connection.execute(text(sql))
                # attempt状态不属于不可变保护；合法UPDATE仍能执行，事务回滚不改库存。
                for suffix in ("receivable", "order", "rea"):
                    tx = connection.begin()
                    try:
                        assert connection.execute(text(f"UPDATE ops.enterprise_{suffix}_attempt SET state='failed'")).rowcount == 1
                    finally: tx.rollback()
            assert_source_unchanged(restored, expected)
        finally: restored.dispose()
    monkeypatch.setattr(service, "open_restore_target", lambda options: disposable_restore_target(options, created=created, after=check_rows))
    result = service.verify_backup(settings, backup_id=identifier)
    assert result.verification_level == "restore_verified" and result.table_count == len(expected.tables)
    assert len(created) == 1
    with pytest.raises(Exception): created[0].reload()
    assert_source_unchanged(engine, expected)


def test_source_guard_mutations_fail_closed_and_rollback(source):
    _, engine, expected, _, _ = source
    for change in DAMAGE:
        with engine.connect() as connection:
            tx = connection.begin()
            try:
                for sql in mutation_statements(change): connection.execute(text(sql))
                with pytest.raises(BackupError) as caught: inventory._catalog(connection, inventory.Budget(30))
                assert caught.value.reason_code == "unsupported_database_objects" and caught.value.exit_code == 2
            finally: tx.rollback()
    assert_source_unchanged(engine, expected)


@pytest.mark.parametrize("change", DAMAGE)
def test_modified_restored_guards_never_verified(source, monkeypatch, change):
    settings, engine, expected, identifier, _ = source
    created = []
    monkeypatch.setattr(service, "open_restore_target", lambda options: disposable_restore_target(options, created=created))
    store = BackupStore(backup_options(settings).path)
    if store.read_verification(identifier) is None:
        service.verify_backup(settings, backup_id=identifier)
        created.clear()
    before = store.read_verification(identifier)
    original = DockerTools.restore
    def altered(self, target, archive, timeout):
        original(self, target, archive, timeout)
        restored = service.open_source_engine(target)
        try:
            with restored.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off")); connection.commit()
                with connection.begin():
                    for sql in mutation_statements(change): connection.execute(text(sql))
        finally: restored.dispose()
    monkeypatch.setattr(DockerTools, "restore", altered)
    assert before is not None  # 真实成功回执不能被篡改失败覆盖；也可独立运行本测试。
    with pytest.raises(BackupError) as caught: service.verify_backup(settings, backup_id=identifier)
    assert caught.value.reason_code == "restored_inventory_mismatch" and caught.value.exit_code == 4
    assert store.read_verification(identifier) == before and len(created) == 1
    with pytest.raises(Exception): created[0].reload()
    assert_source_unchanged(engine, expected)


def test_release_restored_guard_rejection_is_verification_failure(source, monkeypatch):
    from app.ops import release
    settings, engine, expected, identifier, _ = source
    original = DockerTools.restore
    calls, created = [], []
    def altered(self, target, archive, timeout):
        original(self, target, archive, timeout)
        calls.append(True)
        if len(calls) != 1: return  # 原归档在另一个自有副本的恢复必须正常。
        restored = service.open_source_engine(target)
        try:
            with restored.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off")); connection.commit()
                connection.execute(text("ALTER TABLE raw.enterprise_receivable_response DISABLE TRIGGER immutable_enterprise_receivable"))
                connection.commit()
        finally: restored.dispose()
    monkeypatch.setattr(DockerTools,"restore",altered)
    monkeypatch.setattr(release.service,"open_restore_target",lambda options:disposable_restore_target(options,created=created))
    path = backup_options(settings).path / identifier
    report = release.verify_release(settings,package=path)
    assert report.status == "failed" and report.exit_code == 4 and report.recovery_verified is True
    assert report.reason_code == "restored_inventory_mismatch"
    assert any(s.stage=="restore_fingerprints" and s.status=="failed" for s in report.audit)
    assert len(created)==2 and len(calls)==2
    for container in created:
        with pytest.raises(Exception): container.reload()
    assert_source_unchanged(engine,expected)
