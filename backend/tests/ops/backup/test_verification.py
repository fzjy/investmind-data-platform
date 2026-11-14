from __future__ import annotations

from contextlib import contextmanager
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ops.backup.config import parse_target
from app.ops.backup.contracts import BackupError, DatabaseInventory, PublicationInventory, TableInventory
from app.ops.backup.files import BackupStore
from backend.tests.ops.backup.test_files import publish
from backend.tests.ops.backup.test_service import module, settings


class ControlledRestore:
    def __init__(self) -> None:
        self.inventory = DatabaseInventory(tables=())
        self.fail_restore = False
        self.fail_sequence = False
        self.fail_api = False
        self.cleaned = False
        self.probed = False

    @contextmanager
    def target(self, options):
        try:
            yield SimpleNamespace(target=parse_target("postgresql://restore:random@controlled-restore/db"), container_id="b" * 64, owner_token="unit", assert_owned=lambda: None)
        finally:
            self.cleaned = True

    @contextmanager
    def tools(self, options):
        yield self

    def versions(self):
        return "pg_dump (PostgreSQL) 17.11", "pg_restore (PostgreSQL) 17.11"

    def list_archive(self, archive, timeout):
        assert archive.read_bytes() == b"PGDMP\x00\xff\r\n"

    def restore(self, target, archive, timeout):
        assert target.host == "controlled-restore" and timeout > 0
        if self.fail_restore:
            raise BackupError("tool_failed", 3, "restore")

    @contextmanager
    def snapshot(self, engine, *, timeout):
        yield SimpleNamespace(connection=self, server_major=17)

    def dispose(self):
        pass

    def sequence(self, connection, inventory):
        if self.fail_sequence:
            raise BackupError("unsafe_restored_sequence", 4, "verification")

    def api(self, target, expected, *, timeout):
        self.probed = True
        if self.fail_api:
            raise BackupError("restored_api_mismatch", 4, "verification")
        return {"status": "ok"}

    def install(self, service, monkeypatch):
        monkeypatch.setattr(service, "open_restore_target", self.target)
        monkeypatch.setattr(service, "open_tools", self.tools)
        monkeypatch.setattr(service, "open_source_engine", lambda target: self)
        monkeypatch.setattr(service, "snapshot_inventory", self.snapshot)
        monkeypatch.setattr(service, "read_inventory", lambda connection, *, timeout: self.inventory)
        monkeypatch.setattr(service, "check_sequences", self.sequence)
        monkeypatch.setattr(service, "probe_restored_api", self.api)


@pytest.mark.parametrize("failure", ["restore", "rows", "sequence", "api"])
def test_restore_failure_does_not_write_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    service = module()
    assert hasattr(service, "verify_backup"), "verification workflow is missing"
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    fake = ControlledRestore()
    fake.fail_restore = failure == "restore"
    fake.fail_sequence = failure == "sequence"
    fake.fail_api = failure == "api"
    if failure == "rows":
        fake.inventory = DatabaseInventory(tables=(TableInventory(schema_name="core", table_name="security", row_count=0, rows_sha256="a" * 64, schema_sha256="b" * 64),))
    fake.install(service, monkeypatch)
    with pytest.raises(BackupError):
        service.verify_backup(settings(root), backup_id=identifier)
    assert not (path / "verification.json").exists()
    assert fake.cleaned


def test_success_binds_manifest_and_archive_after_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    assert hasattr(service, "verify_backup"), "verification workflow is missing"
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    fake = ControlledRestore()
    fake.install(service, monkeypatch)
    report = service.verify_backup(settings(root), backup_id=identifier)
    assert fake.probed and fake.cleaned
    assert report.verification_level == "restore_verified"
    assert report.manifest_sha256 is not None and report.archive_sha256 is not None
    assert (path / "verification.json").exists()


@pytest.mark.parametrize("change", ["rows_sha256", "schema_sha256", "publication"])
def test_same_count_bad_fingerprint_or_schema_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    service = module()
    assert hasattr(service, "verify_backup")
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    table = TableInventory(schema_name="ops", table_name="job_failure", row_count=37, rows_sha256="a" * 64, schema_sha256="b" * 64)
    publication = PublicationInventory(product="macro-insights", version="old-version", stale=True)
    expected = DatabaseInventory(tables=(table,), publications=(publication,))
    old = BackupStore(root).load(identifier)
    (path / "manifest.json").write_text(old.model_copy(update={"inventory": expected}).model_dump_json(by_alias=True), encoding="utf-8")
    fake = ControlledRestore()
    if change == "publication":
        fake.inventory = expected.model_copy(update={"publications": (publication.model_copy(update={"version": "bad-version"}),)})
    else:
        fake.inventory = expected.model_copy(update={"tables": (table.model_copy(update={change: "f" * 64}),)})
    fake.install(service, monkeypatch)
    with pytest.raises(BackupError) as caught:
        service.verify_backup(settings(root), backup_id=identifier)
    assert caught.value.exit_code == 4
    assert not fake.probed
    assert not (path / "verification.json").exists()


def test_degraded_failures_preserved_after_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    expected = DatabaseInventory(tables=(TableInventory(schema_name="ops", table_name="job_failure", row_count=37, rows_sha256="a" * 64, schema_sha256="b" * 64),),
        publications=(PublicationInventory(product="macro-insights", version="old-version", stale=True),))
    old = BackupStore(root).load(identifier)
    (path / "manifest.json").write_text(old.model_copy(update={"inventory": expected}).model_dump_json(by_alias=True), encoding="utf-8")
    fake = ControlledRestore()
    fake.inventory = expected
    fake.install(service, monkeypatch)
    result = service.verify_backup(settings(root), backup_id=identifier)
    assert result.verification_level == "restore_verified"
    assert BackupStore(root).load(identifier).inventory == expected
    assert fake.inventory.tables[0].row_count == 37


def test_manifest_mutation_during_api_check_is_not_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    fake = ControlledRestore()
    fake.install(service, monkeypatch)
    def mutate(target, expected, *, timeout):
        old = BackupStore(root).load(identifier)
        (path / "manifest.json").write_text(old.model_copy(update={"code_head": "f" * 40}).model_dump_json(by_alias=True), encoding="utf-8")
        return {"status": "ok"}
    monkeypatch.setattr(service, "probe_restored_api", mutate)
    with pytest.raises(BackupError) as caught:
        service.verify_backup(settings(root), backup_id=identifier)
    assert caught.value.exit_code == 4
    assert not (path / "verification.json").exists()


@pytest.mark.parametrize("stage,reason,code,wanted", [("snapshot","unsupported_database_objects",2,4),
    ("inventory","unsupported_database_objects",2,4), ("empty","unsupported_database_objects",2,2),
    ("snapshot","backup_budget_exhausted",3,3), ("inventory","database_inventory_failed",3,3)])
def test_restored_guard_rejection_is_mismatch(tmp_path, monkeypatch, stage, reason, code, wanted):
    service = module()
    root = tmp_path / "archive"
    identifier, path = publish(BackupStore(root))
    fake = ControlledRestore()
    fake.install(service, monkeypatch)
    calls = []
    @contextmanager
    def snapshot(engine, *, timeout):
        calls.append(True)
        if (stage == "empty" and len(calls) == 1) or (stage == "snapshot" and len(calls) == 2):
            raise BackupError(reason, code, "inventory")
        yield SimpleNamespace(connection=fake, server_major=17)
    def read(connection, *, timeout): raise BackupError(reason, code, "inventory")
    monkeypatch.setattr(service, "snapshot_inventory", snapshot)
    if stage == "inventory": monkeypatch.setattr(service, "read_inventory", read)
    previous = b'{"existing":"must-not-overwrite"}'
    (path / "verification.json").write_bytes(previous)
    with pytest.raises(BackupError) as caught: service.verify_backup(settings(root), backup_id=identifier)
    assert caught.value.exit_code == wanted
    assert caught.value.reason_code == ("restored_inventory_mismatch" if wanted == 4 else reason)
    assert not fake.probed and fake.cleaned
    assert (path / "verification.json").read_bytes() == previous


@pytest.mark.parametrize("reason,code,wanted", [("unsupported_database_objects",2,4),("backup_budget_exhausted",3,3),("restore_target_ownership_mismatch",2,2)])
def test_release_copy_catalog_rejection_is_verification_not_source_configuration(monkeypatch, reason, code, wanted):
    from app.ops import release
    assert hasattr(release,"_verified_copy_snapshot"), "release post-upgrade snapshot classification missing"
    @contextmanager
    def rejected(*args, **kwargs):
        raise BackupError(reason,code,"inventory")
        yield
    monkeypatch.setattr(release.service,"snapshot_inventory",rejected)
    with pytest.raises(BackupError) as caught:
        with release._verified_copy_snapshot(None,timeout=5,stage="version_acceptance"): pytest.fail("accepted bad copy")
    assert caught.value.exit_code == wanted
    assert caught.value.reason_code == ("restored_inventory_mismatch" if wanted==4 else reason)
    assert caught.value.stage == ("version_acceptance" if wanted==4 else "inventory")
