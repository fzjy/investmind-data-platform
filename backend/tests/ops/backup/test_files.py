from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from app.config import PROJECT_ROOT
from app.ops.backup.contracts import BackupError, BackupManifest, BackupResult, DatabaseInventory


def file_module():
    assert importlib.util.find_spec("app.ops.backup.files") is not None, "backup storage is not implemented"
    return importlib.import_module("app.ops.backup.files")


def backup_id() -> str:
    return "20200101T000000Z-" + uuid.uuid4().hex


def manifest(identifier: str, content: bytes = b"PGDMP\x00\xff\r\n") -> BackupManifest:
    return BackupManifest(
        backup_id=identifier, source_identity="a" * 64,
        completed_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
        archive_sha256=hashlib.sha256(content).hexdigest(), archive_size=len(content),
        tool_mode="docker", dump_version="pg_dump (PostgreSQL) 17.11",
        restore_version="pg_restore (PostgreSQL) 17.11", server_major=17,
        code_head="4" * 40, inventory=DatabaseInventory(tables=()),
    )


def publish(store, identifier: str | None = None):
    identifier = identifier or backup_id()
    stage = store.stage(identifier)
    (stage / "database.dump").write_bytes(b"PGDMP\x00\xff\r\n")
    return identifier, store.promote(identifier, manifest(identifier))


def test_legacy_format1_roundtrip_unchanged():
    # 旧格式的独立字面量，不由新版model先生成期望值。
    legacy = '{"formatVersion":1,"backupId":"20200101T000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sourceIdentity":"' + 'a'*64 + '","completedAt":"2020-01-02T00:00:00Z","archiveSha256":"' + 'b'*64 + '","archiveSize":9,"toolMode":"docker","dumpVersion":"pg_dump (PostgreSQL) 17.11","restoreVersion":"pg_restore (PostgreSQL) 17.11","serverMajor":17,"codeHead":"' + '4'*40 + '","inventory":{"tables":[],"schemas":[],"alembicVersions":[],"publications":[],"sequences":[]}}'
    legacy = legacy.replace('"toolMode"', '"archiveFormat":"custom","toolMode"')
    parsed = BackupManifest.model_validate_json(legacy)
    assert parsed.model_dump_json(by_alias=True) == legacy


def test_partial_never_selected(tmp_path: Path) -> None:
    module = file_module()
    store = module.BackupStore(tmp_path / "archive")
    stage = store.stage(backup_id())
    (stage / "database.dump").write_bytes(b"unfinished")
    (store.root / "unrelated").mkdir()
    assert store.scan() == ()


def test_atomic_promote_and_corrupt_archive(tmp_path: Path) -> None:
    store = file_module().BackupStore(tmp_path / "带 空格 archive")
    identifier, path = publish(store)
    assert not (store.root / (".partial-" + identifier)).exists()
    assert [package.backup_id for package in store.scan()] == [identifier]
    assert store.load(identifier).archive_size == 9
    with pytest.raises(BackupError):
        store.stage(identifier)
    (path / "database.dump").write_bytes(b"PGDMP-corrupt")
    with pytest.raises(BackupError) as caught:
        store.load(identifier)
    assert caught.value.exit_code == 4
    assert store.scan() == ()


@pytest.mark.parametrize(("field", "value"), [
    ("formatVersion", 2), ("formatVersion", True), ("backupId", "../../other"),
    ("completedAt", "2020-01-02T00:00:00"),
    ("completedAt", "2019-01-01T00:00:00Z"),
    ("archiveSize", 0), ("path", "../../other"),
])
def test_manifest_version_id_and_future_time_rejected(tmp_path: Path, field: str, value: object) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    data[field] = value
    (path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BackupError):
        store.load(identifier)


def test_future_completion_cannot_hide_due_backup(tmp_path: Path) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    data["completedAt"] = (datetime.now(timezone.utc) + timedelta(days=100)).isoformat()
    (path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BackupError) as caught:
        store.scan()
    assert caught.value.reason_code == "future_manifest_time"


@pytest.mark.parametrize("identifier", ["../escape", "C:/external", ".partial-test", "20200101T000000Z-" + "A" * 32])
def test_package_operations_never_accept_paths(tmp_path: Path, identifier: str) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    for operation in (store.load, store.stage, store.delete_package):
        with pytest.raises(BackupError):
            operation(identifier)
    assert not store.root.exists()


@pytest.mark.parametrize("root", [PROJECT_ROOT, PROJECT_ROOT.parent, PROJECT_ROOT / ".git", PROJECT_ROOT / "backend" / "app", Path.home(), Path(Path.cwd().anchor)])
def test_wide_or_code_roots_are_rejected(root: Path) -> None:
    module = file_module()
    with pytest.raises(BackupError) as caught:
        module.BackupStore(root)
    assert caught.value.exit_code == 2


def test_unknown_members_block_deletion_without_partial_remove(tmp_path: Path) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    (path / "personal.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError):
        store.delete_package(identifier)
    assert (path / "database.dump").exists()
    assert (path / "personal.txt").read_text() == "keep"


def test_reparse_replacement_blocks_delete(tmp_path: Path) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "database.dump"
    sentinel.write_bytes(b"outside-canary")
    path.rename(store.root / "preserved-original")
    if os.name == "nt":
        # 使用 Windows 原生 API 创建 junction；不需要管理员 symlink 权限。
        from backend.tests.ops.backup.windows_junction import make_junction
        make_junction(path, outside)
    else:
        path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackupError):
        store.delete_package(identifier)
    assert sentinel.read_bytes() == b"outside-canary"


def test_linked_ancestor_and_hardlinked_member_blocked(tmp_path: Path) -> None:
    module = file_module()
    store = module.BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    original = path / "database.dump"
    os.link(original, tmp_path / "hardlink.dump")
    with pytest.raises(BackupError):
        store.load(identifier)
    linked_root = tmp_path / "linked"
    if os.name == "nt":
        from backend.tests.ops.backup.windows_junction import make_junction
        make_junction(linked_root, store.root)
    else:
        linked_root.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(BackupError):
        module.BackupStore(linked_root / "child")


def test_verification_is_bound_to_both_hashes(tmp_path: Path) -> None:
    store = file_module().BackupStore(tmp_path / "archive")
    identifier, path = publish(store)
    completed = datetime.now(timezone.utc)
    report = BackupResult(
        status="completed", stage="verification", reason_code="restore_verified",
        backup_id=identifier, tool_mode="docker", started_at=completed, ended_at=completed,
        verification_level="restore_verified", archive_sha256=manifest(identifier).archive_sha256,
        manifest_sha256=hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest(),
    )
    store.write_verification(identifier, report)
    assert json.loads((path / "verification.json").read_text())["verificationLevel"] == "restore_verified"
    with pytest.raises(BackupError):
        store.write_verification(identifier, report.model_copy(update={"manifest_sha256": "0" * 64}))
    store.delete_package(identifier)
    assert not path.exists()


def test_lock_is_process_owned_and_crash_releases(tmp_path: Path) -> None:
    module = file_module()
    root = tmp_path / "archive"
    script = (
        "import sys, time; from pathlib import Path; "
        f"sys.path.insert(0, {str(PROJECT_ROOT / 'backend')!r}); "
        "from app.ops.backup.files import backup_lock; "
        "lock=backup_lock(Path(sys.argv[1])); lock.__enter__(); "
        "print('locked', flush=True); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-I", "-c", script, str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(BackupError) as caught:
            with module.backup_lock(root):
                pytest.fail("second owner entered")
        assert caught.value.exit_code == 5
        child.kill()
        child.wait(timeout=10)
        with module.backup_lock(root):
            assert (root / ".backup.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
