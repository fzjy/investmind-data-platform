"""发布入口的只读、拒绝输入和失败报告边界。"""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from app.config import Settings
from app.ops.backup.contracts import BackupError
from app.ops.backup.files import BackupStore
from backend.tests.ops.backup.test_files import publish


def release_module():
    assert importlib.util.find_spec("app.ops.release") is not None, "缺少真实发布验收实现"
    return importlib.import_module("app.ops.release")


def cli_app():
    assert importlib.util.find_spec("app.cli.release") is not None, "缺少 Typer 发布命令"
    app = typer.Typer()
    importlib.import_module("app.cli.release").register_release_commands(app)
    return app


def package(tmp_path, *, revision="20260920_0009"):
    store = BackupStore(tmp_path / "packages")
    identifier, path = publish(store)
    manifest = store.load(identifier)
    inventory = manifest.inventory.model_copy(update={"alembic_versions": (revision,)})
    (path / "manifest.json").write_text(manifest.model_copy(update={"inventory": inventory}).model_dump_json(by_alias=True), encoding="utf-8")
    return path


def test_plan_is_local_read_only_and_explains_backup_restore(tmp_path, monkeypatch):
    module = release_module()
    path = package(tmp_path)
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail("生产计划不得连接数据库或启动工具")
    monkeypatch.setattr(module.service, "open_source_engine", forbidden)
    monkeypatch.setattr(module.service, "open_restore_target", forbidden)
    report = module.release_plan(package=path)
    assert report.status == "planned"
    assert report.source_versions == ("20260920_0009",)
    assert report.target_versions == ("20261115_0021",)
    assert report.migrations == ("20260929_0010", "20261014_0011", "20261015_0012", "20261018_0013", "20261020_0014", "20261021_0015", "20261022_0016", "20261022_0017",
                                 "20261110_0018", "20261111_0019", "20261111_0020", "20261115_0021")
    assert report.recovery_strategy == "backup_restore"
    assert report.steps.index("stop_jobs") < report.steps.index("backup") < report.steps.index("verify_copy") < report.steps.index("upgrade") < report.steps.index("read_acceptance")
    assert report.recovery_steps == ("stop_jobs", "restore_backup_to_new_database", "verify_original_fingerprints", "switch_database_and_previous_code", "read_acceptance", "resume_jobs")
    assert before == {file.name: file.read_bytes() for file in path.iterdir()}
    assert not (path.parent / ".backup.lock").exists()


def test_plan_without_package_does_not_claim_database_version():
    report = release_module().release_plan()
    assert report.source_versions == ()
    assert report.migrations == ()
    assert report.version_evidence == "not_read"
    assert report.production_executed is False


@pytest.mark.parametrize("revision", ["unknown-version", "head", "base"])
def test_unknown_or_symbolic_backup_revision_is_rejected(tmp_path, revision):
    with pytest.raises(BackupError) as error:
        release_module().release_plan(package=package(tmp_path, revision=revision))
    assert error.value.exit_code == 2


def test_corrupt_package_fails_before_any_restore_target(tmp_path, monkeypatch):
    module = release_module()
    path = package(tmp_path)
    (path / "database.dump").write_bytes(b"corrupt")
    def forbidden(*args, **kwargs):
        pytest.fail("损坏归档不得创建容器")
    monkeypatch.setattr(module.service, "open_restore_target", forbidden)
    report = module.verify_release(Settings(_env_file=None), package=path)
    assert report.status == "failed"
    assert report.exit_code == 4
    assert report.recovery_strategy == "backup_restore"
    assert not any(step.stage == "upgrade" and step.status == "completed" for step in report.audit)


@pytest.mark.parametrize("value", ["postgresql://business:password@localhost/prod", "https://example.test/package"])
def test_package_url_is_rejected_without_leaking_credentials(value):
    report = release_module().verify_release(Settings(_env_file=None), package=value)
    assert report.status == "blocked"
    assert report.exit_code == 2
    assert "password" not in report.model_dump_json()
    assert "localhost" not in report.model_dump_json()


def test_cli_plan_ignores_invalid_production_configuration(monkeypatch):
    app = cli_app()
    monkeypatch.setenv("DANG_DATABASE_URL", "invalid-business-url")
    monkeypatch.setenv("DANG_BACKUP_INTERVAL_DAYS", "invalid")
    result = CliRunner().invoke(app, ["release-plan"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "planned"
    assert report["productionExecuted"] is False
    assert report["recoveryStrategy"] == "backup_restore"


@pytest.mark.parametrize("args", [[], ["--database-url", "postgresql://user:TOP_SECRET@business/prod"], ["--package", "postgresql://user:TOP_SECRET@business/prod"], ["--package"]])
def test_cli_verification_refuses_missing_package_and_business_url(args):
    result = CliRunner().invoke(cli_app(), ["verify-release", *args])
    assert result.exit_code == 2
    assert "TOP_SECRET" not in result.output
    assert "business/prod" not in result.output


def test_cli_failure_is_json_with_safe_reason(tmp_path):
    result = CliRunner().invoke(cli_app(), ["verify-release", "--package", str(tmp_path / "missing")])
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert report["status"] == "blocked"
    assert report["recoveryStrategy"] == "backup_restore"


def test_upgrade_helper_refuses_unowned_target():
    module = release_module()
    class Unowned:
        def assert_owned(self):
            raise BackupError("restore_target_ownership_mismatch", 2, "target")
    with pytest.raises(BackupError) as error:
        module._upgrade_copy(Unowned(), timeout=30)
    assert error.value.reason_code == "restore_target_ownership_mismatch"


def test_plan_report_is_associated_with_shared_observability():
    from app.ops.observability import install_cli_observability
    app = cli_app()
    install_cli_observability(app, settings_loader=lambda: Settings(_env_file=None))
    result = CliRunner().invoke(app, ["release-plan"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "planned"
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert events[-1]["command"] == "release-plan"
    assert events[-1]["state"] == "planned"
