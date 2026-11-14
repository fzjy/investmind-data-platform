from __future__ import annotations

from datetime import datetime, timezone
import importlib
import importlib.util

import pytest
from typer.testing import CliRunner

from app.config import Settings
from app.ops.backup.contracts import BackupError, BackupResult


def modules():
    assert importlib.util.find_spec("app.cli.backup") is not None, "backup CLI is missing"
    return importlib.import_module("app.cli.backup"), importlib.import_module("app.cli.main")


@pytest.mark.parametrize(("command", "operation"), [
    ("backup-database", "create_backup"), ("verify-database-backup", "verify_backup"),
    ("backup-status", "read_backup_status"), ("prune-database-backups", "prune_backups"),
])
@pytest.mark.parametrize("code", [0, 2, 3, 4, 5])
def test_command_exit_codes_are_stable_and_safe(command: str, operation: str, code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    backup, main = modules()
    monkeypatch.setattr(backup.Settings, "from_env", lambda: Settings(_env_file=None))
    def controlled(settings, **kwargs):
        if code:
            raise BackupError("controlled_failure", code, "controlled")
        stamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
        return BackupResult(status="skipped", stage="backup", reason_code="backup_not_due", tool_mode="docker", started_at=stamp, ended_at=stamp)
    monkeypatch.setattr(backup, operation, controlled)
    result = CliRunner().invoke(main.app, [command])
    assert result.exit_code == code
    assert "reasonCode" in result.stdout
    assert "secret-canary" not in result.output


def test_configuration_error_does_not_echo_raw_values(monkeypatch: pytest.MonkeyPatch) -> None:
    backup, main = modules()
    def bad_config():
        raise ValueError("postgresql://secret-canary")
    monkeypatch.setattr(backup.Settings, "from_env", bad_config)
    result = CliRunner().invoke(main.app, ["backup-status"])
    assert result.exit_code == 2
    assert "invalid_backup_configuration" in result.stdout
    assert "secret-canary" not in result.output


@pytest.mark.parametrize("command", ["backup-database", "verify-database-backup", "backup-status", "prune-database-backups"])
def test_unknown_url_arguments_are_not_echoed(command: str) -> None:
    _, main = modules()
    result = CliRunner().invoke(main.app, [command, "--target-url", "postgresql://u:secret-canary@business/db"])
    assert result.exit_code == 2
    assert "secret-canary" not in result.output
    assert "postgresql" not in result.output
