"""真实 pg_dump/pg_restore，源与目标均由隔离运行器登记。"""
import os
import shutil

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import PROJECT_ROOT, Settings
from app.ops.backup import service
from app.ops.backup.config import backup_options, parse_target
from app.ops.backup.contracts import BackupError
from app.ops.backup.files import BackupStore
from app.ops.backup.inventory import assert_restored_inventory, read_inventory, snapshot_inventory
from app.ops.backup.tools import DockerTools, open_tools
from backend.tests.e2e.controlled_backup import seed_backup_database, disposable_restore_target

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="requires registered disposable PostgreSQL17")]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("backup source 空格")
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        previous = vars(config_module).get("settings")
        config_module.settings = Settings(database_url=url)
        try:
            command.upgrade(Config(str(PROJECT_ROOT / "backend/alembic.ini")), "head")
        finally:
            if previous is None:
                del config_module.settings
            else:
                config_module.settings = previous
        engine = create_engine(url)
        try:
            inventory = seed_backup_database(sessionmaker(engine, expire_on_commit=False))
            settings = Settings(database_url=url, backup_path=str(root / "packages"))
            result = service.create_backup(settings, force=True)
            yield settings, engine, inventory, result.backup_id
        finally:
            engine.dispose()


def test_real_restore_verifies_all_products_and_preserves_source(source, monkeypatch):
    settings, engine, inventory, identifier = source
    created = []
    monkeypatch.setattr(service, "open_restore_target", lambda options: disposable_restore_target(options, created=created))
    result = service.verify_backup(settings, backup_id=identifier)
    assert result.verification_level == "restore_verified"
    assert result.table_count == len(inventory.tables)
    assert {(t.schema_name,t.table_name) for t in inventory.tables} >= {("raw","enterprise_target_observation"),("core","enterprise_target_candidate")}
    with snapshot_inventory(engine, timeout=120) as view:
        assert read_inventory(view.connection, timeout=120) == inventory
    manifest = BackupStore(backup_options(settings).path).load(identifier)
    assert manifest.inventory == inventory
    assert {(item.product, item.row_count) for item in inventory.publications if item.product == "screener"} == {("screener", 50)}
    assert {item.product for item in inventory.publications} >= {"macro-transmission", "macro-insights"}
    assert next(item for item in inventory.tables if item.table_name == "content_document").row_count == 0
    assert next(item for item in inventory.tables if item.table_name == "job_failure").row_count == 1
    assert len(created) == 1
    with pytest.raises(Exception):
        created[0].reload()


@pytest.mark.parametrize("change", ["value", "constraint", "sequence", "schema"])
def test_modified_restore_never_verified(source, monkeypatch, change):
    settings, _, _, identifier = source
    original = DockerTools.restore
    def altered(self, target, archive, timeout):
        original(self, target, archive, timeout)
        engine = service.open_source_engine(target)
        try:
            with engine.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off"))
                connection.commit()
                with connection.begin():
                    sql = {"value": "UPDATE core.security SET name='changed' WHERE ts_code='600519.SH'",
                           "constraint": "ALTER TABLE core.security DROP CONSTRAINT pk_security CASCADE",
                           "sequence": "SELECT setval('raw.source_payload_id_seq', 1, false)",
                           "schema": "CREATE SCHEMA unrecorded_empty"}[change]
                    connection.execute(text(sql))
        finally:
            engine.dispose()
    monkeypatch.setattr(DockerTools, "restore", altered)
    monkeypatch.setattr(service, "open_restore_target", disposable_restore_target)
    store = BackupStore(backup_options(settings).path)
    before = store.read_verification(identifier)
    with pytest.raises(BackupError) as error:
        service.verify_backup(settings, backup_id=identifier)
    assert error.value.exit_code == 4
    assert store.read_verification(identifier) == before


def test_corrupt_archive_is_rejected_before_target_creation(source, tmp_path, monkeypatch):
    settings, _, _, identifier = source
    copy = tmp_path / "damaged-packages"
    shutil.copytree(backup_options(settings).path / identifier, copy / identifier)
    archive = copy / identifier / "database.dump"
    data = archive.read_bytes()
    archive.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    def forbidden_target(options):
        raise AssertionError("corrupt archive must not create target")
    monkeypatch.setattr(service, "open_restore_target", forbidden_target)
    with pytest.raises(BackupError) as error:
        service.verify_backup(settings.model_copy(update={"backup_path": str(copy)}), backup_id=identifier)
    assert error.value.exit_code == 4


def test_single_transaction_restore_rolls_back_on_collision(source):
    settings, _, _, identifier = source
    archive = backup_options(settings).path / identifier / "database.dump"
    def collision(target):
        engine = service.open_source_engine(target)
        try:
            with engine.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off"))
                connection.commit()
                connection.execute(text("CREATE SCHEMA mart"))
                connection.commit()
        finally:
            engine.dispose()
    with disposable_restore_target(backup_options(settings), before=collision) as restored, open_tools(backup_options(settings)) as tools:
        with pytest.raises(BackupError) as error:
            tools.restore(restored.target, archive, 120)
        assert error.value.exit_code == 3
        engine = service.open_source_engine(restored.target)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='core'")) == 0
                assert connection.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname='mart'")) == 1
        finally:
            engine.dispose()


def test_exported_snapshot_survives_concurrent_commit(source, tmp_path):
    settings, engine, _, _ = source
    target = parse_target(settings.database_url)
    archive = tmp_path / "concurrent.dump"
    with snapshot_inventory(engine, timeout=120) as view, open_tools(backup_options(settings)) as tools:
        inventory = read_inventory(view.connection, timeout=120)
        with engine.begin() as writer:
            writer.execute(text("INSERT INTO core.security(ts_code,symbol,name,exchange,status,is_st,source,data) VALUES ('888888.SH','888888','after snapshot','SSE','L',false,'controlled','{}')"))
        tools.dump(target, view.snapshot_id, archive, 120)
        with disposable_restore_target(backup_options(settings)) as restored:
            tools.restore(restored.target, archive, 120)
            recovered = service.open_source_engine(restored.target)
            try:
                with snapshot_inventory(recovered, timeout=120) as restored_view:
                            assert_restored_inventory(restored_view.connection, read_inventory(restored_view.connection, timeout=120), inventory, timeout=120)
            finally:
                recovered.dispose()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT name FROM core.security WHERE ts_code='888888.SH'")) == "after snapshot"
    # 只读反例用独立事务，不能让故意中止的事务污染并发导出证明。
    with pytest.raises(BackupError) as error:
        with snapshot_inventory(engine, timeout=120) as view:
            view.connection.execute(text("UPDATE core.security SET name='forbidden'"))
    assert error.value.reason_code == "source_snapshot_failed"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT name FROM core.security WHERE ts_code='888888.SH'")) == "after snapshot"


def test_worker_api_mismatch_preserves_verification_exit_code(source):
    settings, _, inventory, identifier = source
    archive = backup_options(settings).path / identifier / "database.dump"
    publications = tuple(item.model_copy(update={"row_count": 51}) if item.product == "screener" else item for item in inventory.publications)
    with disposable_restore_target(backup_options(settings)) as restored, open_tools(backup_options(settings)) as tools:
        tools.restore(restored.target, archive, 120)
        with pytest.raises(BackupError) as error:
            service.probe_restored_api(restored.target, inventory.model_copy(update={"publications": publications}), timeout=60)
        assert error.value.exit_code == 4


def test_dropped_middle_column_real_dump_restore_matches(tmp_path):
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        target = parse_target(url)
        engine = create_engine(url)
        options = backup_options(Settings(_env_file=None))
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE public.legacy (a integer, b text, c text)"))
                connection.execute(text("INSERT INTO public.legacy VALUES (1,'removed','kept')"))
                connection.execute(text("ALTER TABLE public.legacy DROP COLUMN b"))
            with snapshot_inventory(engine, timeout=120) as view, open_tools(options) as tools:
                expected = read_inventory(view.connection, timeout=120)
                archive = tmp_path / "legacy.dump"
                tools.dump(target, view.snapshot_id, archive, 120)
                with disposable_restore_target(options) as restored:
                    tools.restore(restored.target, archive, 120)
                    recovered = service.open_source_engine(restored.target)
                    try:
                        with snapshot_inventory(recovered, timeout=120) as result:
                            assert read_inventory(result.connection, timeout=120) == expected
                    finally:
                        recovered.dispose()
        finally:
            engine.dispose()


@pytest.mark.parametrize("object_sql", [
    "CREATE RULE preserve_rows AS ON DELETE TO public.parent DO INSTEAD NOTHING",
    "CREATE TABLE public.child () INHERITS (public.parent)",
])
def test_unverifiable_rules_and_inheritance_block_before_export(object_sql):
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        engine = create_engine(postgres.get_connection_url(driver="psycopg"))
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE public.parent (id integer)"))
                connection.execute(text(object_sql))
            with pytest.raises(BackupError) as error:
                with snapshot_inventory(engine, timeout=60):
                    pytest.fail("unverifiable catalog was accepted")
            assert error.value.reason_code == "unsupported_database_objects" and error.value.exit_code == 2
        finally:
            engine.dispose()
