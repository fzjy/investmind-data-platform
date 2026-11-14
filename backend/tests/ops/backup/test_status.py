from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.ops.backup.config import parse_target, source_identity
from app.ops.backup.contracts import BackupError, BackupResult
from app.ops.backup.files import BackupStore
from backend.tests.ops.backup.test_retention import packages
from backend.tests.ops.backup.test_service import module, settings


def populate(root: Path, count: int = 10, *, verified: bool = True) -> tuple[str, ...]:
    store = BackupStore(root)
    identity = source_identity(parse_target("postgresql://u:p@unit-source/one"))
    values = packages(count, identity)
    for value in values:
        stage = store.stage(value.backup_id)
        (stage / "database.dump").write_bytes(b"PGDMP\x00\xff\r\n")
        path = store.promote(value.backup_id, value)
        if verified and value in values[-2:]:
            at = value.completed_at + timedelta(minutes=1)
            store.write_verification(value.backup_id, BackupResult(status="completed", stage="verification", reason_code="restore_verified", backup_id=value.backup_id,
                tool_mode="docker", started_at=value.completed_at, ended_at=at, verification_level="restore_verified",
                archive_sha256=value.archive_sha256, manifest_sha256=hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()))
    return tuple(value.backup_id for value in values)


def test_status_is_read_only_and_missing_root_is_not_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = module()
    assert hasattr(service, "read_backup_status"), "read-only backup status is missing"
    root = tmp_path / "missing"
    monkeypatch.setattr(service, "open_tools", lambda options: pytest.fail("status started tools"))
    monkeypatch.setattr(service, "open_source_engine", lambda target: pytest.fail("status connected"))
    result = service.read_backup_status(settings(root))
    assert result.status == "empty" and result.due is True
    assert result.verification_level == "none"
    assert not root.exists()


def test_archive_valid_is_not_restore_verified_and_stale_report_is_rejected(tmp_path: Path) -> None:
    service = module()
    assert hasattr(service, "read_backup_status")
    root = tmp_path / "archive"
    ids = populate(root, 2, verified=False)
    assert service.read_backup_status(settings(root)).verification_level == "archive_valid"
    another = tmp_path / "another"
    populate(another, 2)
    report_path = root / ids[-1] / "verification.json"
    report_path.write_bytes((another / ids[-1] / "verification.json").read_bytes())
    assert service.read_backup_status(settings(root)).verification_level == "restore_verified"
    manifest_path = root / ids[-1] / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["codeHead"] = "f" * 40
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    result = service.read_backup_status(settings(root))
    assert result.verification_level == "archive_valid"
    assert result.latest_verified_id is None
    assert result.anomaly_count == 1


def test_dry_run_never_deletes_and_apply_only_removes_safe_candidates(tmp_path: Path) -> None:
    service = module()
    assert hasattr(service, "prune_backups"), "explicit prune is missing"
    root = tmp_path / "archive"
    ids = populate(root)
    (root / "unknown").mkdir()
    partial = BackupStore(root).stage("20200101T000000Z-" + "f" * 32)
    report = service.prune_backups(settings(root))
    assert report.status == "planned" and report.candidates == ids[:2]
    assert all((root / identifier).exists() for identifier in ids)
    applied = service.prune_backups(settings(root), apply=True)
    assert applied.status == "pruned"
    assert not any((root / identifier).exists() for identifier in ids[:2])
    assert all((root / identifier).exists() for identifier in ids[2:])
    assert partial.exists() and (root / "unknown").exists()


@pytest.mark.parametrize("change", ["junction", "manifest", "verification"])
def test_apply_rechecks_junction_and_changed_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    service = module()
    assert hasattr(service, "prune_backups")
    root = tmp_path / "archive"
    ids = populate(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "personal.txt").write_text("keep", encoding="utf-8")
    original_lock = service.backup_lock
    @contextmanager
    def mutate_before_locked_rescan(path):
        if change == "junction":
            candidate = root / ids[0]
            candidate.rename(root / "original-preserved")
            if os.name == "nt":
                from backend.tests.ops.backup.windows_junction import make_junction
                make_junction(candidate, outside)
            else:
                candidate.symlink_to(outside, target_is_directory=True)
        elif change == "manifest":
            target = root / ids[0] / "manifest.json"
            value = json.loads(target.read_text())
            value["codeHead"] = "f" * 40
            target.write_text(json.dumps(value), encoding="utf-8")
        else:
            (root / ids[-1] / "verification.json").write_text("{}", encoding="utf-8")
        with original_lock(path):
            yield
    monkeypatch.setattr(service, "backup_lock", mutate_before_locked_rescan)
    with pytest.raises(BackupError):
        service.prune_backups(settings(root), apply=True)
    assert (outside / "personal.txt").read_text() == "keep"
    assert all((root / identifier).exists() for identifier in ids[1:])
