from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.ops.backup.config import parse_target, source_identity
from app.ops.backup.contracts import BackupError, DatabaseInventory, TableInventory
from app.ops.backup.files import BackupStore
from backend.tests.ops.backup.test_files import backup_id, manifest


def module():
    assert importlib.util.find_spec("app.ops.backup.service") is not None, "backup orchestration is missing"
    return importlib.import_module("app.ops.backup.service")


def settings(root: Path, database: str = "one") -> Settings:
    return Settings(_env_file=None, database_url=f"postgresql://u:p@unit-source/{database}", backup_path=str(root))


def old_package(root: Path, database: str = "one") -> str:
    store = BackupStore(root)
    identifier = backup_id()
    stage = store.stage(identifier)
    (stage / "database.dump").write_bytes(b"PGDMP\x00\xff\r\n")
    value = manifest(identifier).model_copy(update={"source_identity": source_identity(parse_target(f"postgresql://u:p@unit-source/{database}"))})
    store.promote(identifier, value)
    return identifier


class ControlledBackup:
    def __init__(self) -> None:
        self.active = False
        self.disposed = False
        self.exports = 0
        self.fail_dump = False
        self.fail_inventory = False
        self.server_major = 17
        self.snapshot_id = "00000001-00000002-1"

    def install(self, service, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service, "open_source_engine", lambda target: self)
        monkeypatch.setattr(service, "snapshot_inventory", self.snapshot)
        monkeypatch.setattr(service, "read_inventory", self.inventory)
        monkeypatch.setattr(service, "open_tools", self.tools)
        monkeypatch.setattr(service, "current_code_head", lambda: "4" * 40)

    def dispose(self) -> None:
        self.disposed = True

    @contextmanager
    def snapshot(self, engine, *, timeout):
        assert engine is self and timeout > 0
        self.active = True
        try:
            yield SimpleNamespace(connection=self, snapshot_id=self.snapshot_id, server_major=self.server_major)
        finally:
            self.active = False

    def inventory(self, connection, *, timeout):
        assert self.active and connection is self and timeout > 0
        if self.fail_inventory:
            raise BackupError("inventory_timeout", 3, "inventory")
        return DatabaseInventory(tables=(TableInventory(schema_name="core", table_name="security", row_count=50, rows_sha256="b" * 64, schema_sha256="c" * 64),))

    @contextmanager
    def tools(self, options):
        yield self

    def versions(self):
        return "pg_dump (PostgreSQL) 17.11", "pg_restore (PostgreSQL) 17.11"

    def dump(self, target, snapshot_id, destination, timeout):
        assert self.active and snapshot_id == self.snapshot_id
        assert target.database == "one" and timeout > 0
        self.exports += 1
        destination.write_bytes(b"PGDMP\x00\xff\r\n")
        if self.fail_dump:
            raise BackupError("tool_failed", 3, "dump")

    def list_archive(self, archive, timeout):
        assert self.active and timeout > 0
        assert archive.read_bytes() == b"PGDMP\x00\xff\r\n"


def test_other_source_does_not_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    root = tmp_path / "archive"
    old_package(root, "other")
    fake = ControlledBackup()
    fake.install(service, monkeypatch)
    report = service.create_backup(settings(root), now=datetime(2020, 1, 3, tzinfo=timezone.utc))
    assert report.status == "completed"
    assert fake.exports == 1
    assert report.table_count == 1
    assert len(BackupStore(root).scan()) == 2


@pytest.mark.parametrize(("elapsed", "force", "wanted"), [(timedelta(days=6, hours=23), False, "skipped"), (timedelta(days=7), False, "completed"), (timedelta(days=1), True, "completed")])
def test_due_boundary_force_and_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, elapsed: timedelta, force: bool, wanted: str) -> None:
    service = module()
    root = tmp_path / "archive"
    old_package(root)
    partial = BackupStore(root).stage(backup_id())
    (partial / "database.dump").write_bytes(b"unfinished")
    fake = ControlledBackup()
    fake.install(service, monkeypatch)
    if wanted == "skipped":
        monkeypatch.setattr(service, "open_source_engine", lambda target: pytest.fail("skipped connected to source"))
        monkeypatch.setattr(service, "open_tools", lambda options: pytest.fail("skipped started tools"))
    report = service.create_backup(settings(root), force=force, now=datetime(2020, 1, 2, tzinfo=timezone.utc) + elapsed)
    assert report.status == wanted
    assert fake.exports == (0 if wanted == "skipped" else 1)
    assert partial.exists()


@pytest.mark.parametrize("failure", ["dump", "inventory"])
def test_dump_failure_keeps_partial_and_old_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    service = module()
    root = tmp_path / "archive"
    identifier = old_package(root)
    original = (root / identifier / "manifest.json").read_bytes()
    fake = ControlledBackup()
    fake.fail_dump = failure == "dump"
    fake.fail_inventory = failure == "inventory"
    fake.install(service, monkeypatch)
    with pytest.raises(BackupError):
        service.create_backup(settings(root), force=True)
    assert [value.backup_id for value in BackupStore(root).scan()] == [identifier]
    assert (root / identifier / "manifest.json").read_bytes() == original
    assert any(path.name.startswith(".partial-") for path in root.iterdir())
    assert not fake.active and fake.disposed


def test_snapshot_lives_until_dump_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    fake = ControlledBackup()
    fake.install(service, monkeypatch)
    root = tmp_path / "archive"
    result = service.create_backup(settings(root), force=True)
    assert not fake.active and fake.disposed
    assert result.verification_level == "archive_valid"
    saved = BackupStore(root).load(result.backup_id)
    assert saved.inventory.tables[0].row_count == 50
    assert saved.server_major == 17


def test_force_cannot_bypass_version_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    fake = ControlledBackup()
    fake.server_major = 16
    fake.install(service, monkeypatch)
    root = tmp_path / "archive"
    with pytest.raises(BackupError) as caught:
        service.create_backup(settings(root), force=True)
    assert caught.value.exit_code == 2
    assert fake.exports == 0
    assert BackupStore(root).scan() == ()


def test_future_manifest_blocks_even_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    root = tmp_path / "archive"
    identifier = old_package(root)
    path = root / identifier / "manifest.json"
    import json
    data = json.loads(path.read_text())
    data["completedAt"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(service, "open_source_engine", lambda target: pytest.fail("invalid manifest connected"))
    with pytest.raises(BackupError):
        service.create_backup(settings(root), force=True)
