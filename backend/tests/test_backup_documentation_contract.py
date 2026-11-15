from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_backup_guide_identifies_readonly_status_and_isolated_restore():
    guide = (ROOT/"docs/local-operations.md").read_text(encoding="utf-8")
    for command in ("backup-status", "verify-database-backup --backup-id BACKUP_ID"):
        assert "python -m app.cli.main " + command in guide
    for required in ("database.dump", "manifest.json", "verification.json", "restore_verified", "pg_restore", "--single-transaction"):
        assert required in guide


def test_default_backup_packages_are_ignored_by_git() -> None:
    result = subprocess.run(["git", "check-ignore", "backend/backups/20200101T000000Z-" + "a" * 32 + "/database.dump"], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, "backup archives must never be staged accidentally"
