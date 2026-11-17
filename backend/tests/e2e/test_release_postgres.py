"""发布演练只使用隔离运行器登记的 PostgreSQL 17 副本。"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import importlib.util
import os

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import PROJECT_ROOT, Settings
from app.ops.backup import service
from app.ops.backup.contracts import BackupError
from app.ops.backup.inventory import read_inventory, snapshot_inventory
from backend.tests.e2e.controlled_backup import disposable_restore_target, seed_backup_database

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="需要登记的一次性 PostgreSQL17")]


def migration_heads():
    config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    return tuple(ScriptDirectory.from_config(config).get_heads())


def release_module():
    assert importlib.util.find_spec("app.ops.release") is not None, "缺少真实副本升级实现"
    return importlib.import_module("app.ops.release")


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("release-packages")
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        engine = create_engine(url)
        try:
            # 只在仍支持回退的历史revision造旧包，不越过0012+不可逆保护。
            migration = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
            target_settings = Settings(_env_file=None, database_url=url)
            with pytest.MonkeyPatch.context() as migration_config:
                migration_config.setitem(vars(config_module), "settings", target_settings)
                command.upgrade(migration, "20261014_0011")
            seed_backup_database(sessionmaker(engine, expire_on_commit=False))
            # 仅在测试自有源容器构造真实旧版备份，产品不提供 downgrade。
            with pytest.MonkeyPatch.context() as migration_config:
                migration_config.setitem(vars(config_module), "settings", target_settings)
                command.downgrade(migration, "20260920_0009")
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE mart.published_snapshot ADD COLUMN source_note text DEFAULT 'original'"))
            with snapshot_inventory(engine, timeout=120) as view:
                inventory = read_inventory(view.connection, timeout=120)
            settings = Settings(_env_file=None, database_url=url, backup_path=str(root))
            result = service.create_backup(settings, force=True)
            yield settings, engine, inventory, root / result.backup_id
        finally:
            engine.dispose()


def assert_source_unchanged(engine, expected):
    with snapshot_inventory(engine, timeout=120) as view:
        assert read_inventory(view.connection, timeout=120) == expected


def test_read_actual_versions_is_read_only(source):
    module = release_module()
    _, engine, expected, _ = source
    with engine.connect() as connection:
        assert module.read_alembic_versions(connection, timeout=30) == {"public": ("20260920_0009",)}
    assert_source_unchanged(engine, expected)


def test_real_upgrade_old_backup_checks_api_and_restore_recovery(source, monkeypatch):
    module = release_module()
    settings, engine, expected, path = source
    created = []
    @contextmanager
    def target(options):
        with disposable_restore_target(options, created=created) as restored:
            yield restored
    monkeypatch.setattr(module.service, "open_restore_target", target)
    report = module.verify_release(settings, package=path)
    assert report.status == "completed", report.model_dump_json()
    assert report.source_versions == ("20260920_0009",)
    assert report.target_versions == migration_heads()
    assert report.actual_versions == {"public": migration_heads()}
    assert report.api["stockCount"] == 50
    assert report.api["macroProducts"] == 2
    assert report.recovery_verified is True
    assert {step.stage for step in report.audit if step.status == "completed"} >= {"restore_fingerprints", "upgrade", "version_acceptance", "legacy_data", "api_acceptance", "backup_restore", "cleanup", "package_binding"}
    assert len(created) == 2
    for container in created:
        with pytest.raises(Exception):
            container.reload()
    assert_source_unchanged(engine, expected)


def test_head_package_verification_preserves_source_without_pending_migrations(source, tmp_path, monkeypatch):
    from app.ops.backup.config import backup_options
    module = release_module()
    settings, engine, expected, old_package = source
    monkeypatch.setattr(module.service, "open_restore_target", disposable_restore_target)
    options = backup_options(settings)
    # 由受控旧包构造另一个自有 head 源；真实现有业务包不参与测试。
    with disposable_restore_target(options) as head_source, service.open_tools(options) as tools:
        tools.restore(head_source.target, old_package / "database.dump", 120)
        module._upgrade_copy(head_source, timeout=60)
        head_engine = service.open_source_engine(head_source.target)
        try:
            with snapshot_inventory(head_engine, timeout=120) as view:
                before = read_inventory(view.connection, timeout=120)
            head_settings = settings.model_copy(update={"database_url": head_engine.url.render_as_string(hide_password=False), "backup_path": str(tmp_path / "head-packages")})
            backup = service.create_backup(head_settings, force=True)
            path = tmp_path / "head-packages" / backup.backup_id
            plan = module.release_plan(package=path)
            assert plan.source_versions == plan.target_versions == migration_heads()
            assert plan.migrations == ()
            report = module.verify_release(head_settings, package=path)
            assert report.status == "completed", report.model_dump_json()
            assert report.actual_versions == {"public": migration_heads()}
            assert report.recovery_verified is True
            assert_source_unchanged(head_engine, before)
        finally:
            head_engine.dispose()
    assert_source_unchanged(engine, expected)


@pytest.mark.parametrize("fault", ["migration", "version", "schema", "legacy_data", "legacy_column", "pointer", "history", "api"])
def test_upgrade_fault_never_passes_and_restores_original_backup(source, monkeypatch, fault):
    module = release_module()
    settings, engine, expected, path = source
    monkeypatch.setattr(module.service, "open_restore_target", disposable_restore_target)
    original = module._upgrade_copy
    original_probe = module.service.probe_restored_api
    calls = 0
    def api_fault(target, inventory, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            inventory = inventory.model_copy(update={"publications": tuple(item.model_copy(update={"row_count": 51}) if item.product == "screener" else item for item in inventory.publications)})
        return original_probe(target, inventory, timeout=timeout)
    if fault == "api":
        monkeypatch.setattr(module.service, "probe_restored_api", api_fault)
    def faulty(restored, *, timeout):
        if fault == "migration":
            raise RuntimeError("password=TOP_SECRET business-host")
        original(restored, timeout=timeout)
        copy = service.open_source_engine(restored.target)
        try:
            with copy.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off"))
                connection.commit()
                if fault == "version":
                    connection.execute(text("UPDATE public.alembic_version SET version_num='20260920_0009'"))
                elif fault == "schema":
                    connection.execute(text("ALTER TABLE core.macro_observation DROP COLUMN valid_until"))
                elif fault == "legacy_data":
                    connection.execute(text("UPDATE core.security SET name='tampered' WHERE ts_code='600519.SH'"))
                elif fault == "legacy_column":
                    connection.execute(text("UPDATE mart.published_snapshot SET source_note='altered' WHERE product='screener'"))
                elif fault in {"pointer", "history"}:
                    connection.execute(text("ALTER TABLE mart.published_snapshot DROP CONSTRAINT fk_pointer_history"))
                    if fault == "pointer":
                        connection.execute(text("""UPDATE mart.published_snapshot
                            SET payload=to_json('@investmind-history:' || repeat('0', 32)) WHERE product='screener'"""))
                    else:
                        connection.execute(text("""UPDATE mart.page_snapshot SET payload='{}'
                            WHERE product='screener' AND snapshot_version=(
                                SELECT version FROM mart.published_snapshot WHERE product='screener')"""))
                connection.commit()
        finally:
            copy.dispose()
    monkeypatch.setattr(module, "_upgrade_copy", faulty)
    report = module.verify_release(settings, package=path)
    assert report.status == "failed"
    assert report.exit_code in {3, 4}
    assert report.recovery_verified is True
    expected_stage = {"migration": "upgrade", "version": "version_acceptance", "schema": "schema_acceptance", "legacy_data": "legacy_data", "legacy_column": "legacy_data", "pointer": "legacy_data", "history": "legacy_data", "api": "api_acceptance"}[fault]
    assert any(step.stage == expected_stage and step.status == "failed" for step in report.audit)
    assert "TOP_SECRET" not in report.model_dump_json()
    assert_source_unchanged(engine, expected)


def test_additional_schema_alembic_version_is_read_and_blocks_upgrade(source, monkeypatch):
    module = release_module()
    settings, engine, expected, path = source
    def unexpected_version(target):
        copy = service.open_source_engine(target)
        try:
            with copy.connect() as connection:
                connection.execute(text("SET default_transaction_read_only=off"))
                connection.commit()
                connection.execute(text("CREATE SCHEMA other_app"))
                connection.execute(text("CREATE TABLE other_app.alembic_version (version_num text NOT NULL)"))
                connection.execute(text("INSERT INTO other_app.alembic_version VALUES ('outside-lineage')"))
                connection.commit()
                assert module.read_alembic_versions(connection, timeout=30) == {"public": ("20260920_0009",), "other_app": ("outside-lineage",)}
        finally:
            copy.dispose()
    from app.ops.backup.config import backup_options
    with disposable_restore_target(backup_options(settings)) as restored, service.open_tools(backup_options(settings)) as tools:
        tools.restore(restored.target, path / "database.dump", 120)
        unexpected_version(restored.target)
        with pytest.raises(BackupError) as error:
            module._upgrade_copy(restored, timeout=60)
        assert error.value.reason_code == "unsupported_release_version_layout"
        copy = service.open_source_engine(restored.target)
        try:
            with copy.connect() as connection:
                assert module.read_alembic_versions(connection, timeout=30)["public"] == ("20260920_0009",)
        finally:
            copy.dispose()
    assert_source_unchanged(engine, expected)


def test_failed_recovery_is_explicit_and_never_verified(source, monkeypatch):
    module = release_module()
    settings, engine, expected, path = source
    count = 0
    @contextmanager
    def target(options):
        nonlocal count
        count += 1
        if count == 2:
            raise BackupError("restore_target_not_ready", 3, "target")
        with disposable_restore_target(options) as restored:
            yield restored
    monkeypatch.setattr(module.service, "open_restore_target", target)
    report = module.verify_release(settings, package=path)
    assert report.status == "failed"
    assert report.recovery_verified is False
    assert any(step.stage == "backup_restore" and step.status == "failed" for step in report.audit)
    assert_source_unchanged(engine, expected)
