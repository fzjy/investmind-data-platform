from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.config as app_config
from app.config import PROJECT_ROOT, Settings


def backup_config():
    assert importlib.util.find_spec("app.ops") is not None, "backup configuration is not implemented"
    return importlib.import_module("app.ops.backup.config")


def test_defaults_and_invalid_bounds() -> None:
    module = backup_config()
    options = module.backup_options(Settings(_env_file=None))
    assert options.tool_mode == "docker"
    assert options.path == PROJECT_ROOT / "backend" / "backups"
    assert options.native_bin is None
    assert options.docker_image == "postgres:17-alpine"
    assert (options.interval_days, options.keep_count, options.retention_days, options.timeout_seconds) == (7, 8, 56, 1800)


@pytest.mark.parametrize(("field", "value"), [
    ("backup_tool_mode", "auto-canary"), ("backup_path", ""), ("backup_path", "   "),
    ("backup_native_bin", "secret-canary\n"), ("backup_docker_image", "postgres:latest"),
    ("backup_docker_image", ""), ("backup_interval_days", 0), ("backup_interval_days", 366),
    ("backup_keep_count", 1), ("backup_keep_count", 101), ("backup_retention_days", 0),
    ("backup_retention_days", 3651), ("backup_timeout_seconds", 59), ("backup_timeout_seconds", 7201),
])
def test_invalid_settings_do_not_echo_input(field: str, value: object) -> None:
    assert field in Settings.model_fields, "backup settings are missing"
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: value})
    assert "auto-canary" not in str(caught.value)
    assert "secret-canary" not in str(caught.value)


def test_dotenv_process_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = backup_config()
    env_file = tmp_path / "settings.env"
    env_file.write_text("DANG_BACKUP_INTERVAL_DAYS=12\nDANG_BACKUP_PATH=./local archive\n", encoding="utf-8")
    monkeypatch.setattr(app_config, "PROJECT_ENV_FILE", env_file)
    monkeypatch.setenv("DANG_BACKUP_INTERVAL_DAYS", "9")
    assert module.backup_options(Settings.from_env()).interval_days == 9
    assert module.backup_options(Settings.from_env()).path == PROJECT_ROOT / "local archive"


@pytest.mark.parametrize("origin", ["process", "dotenv"])
def test_explicit_empty_path_is_not_silently_defaulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    assert "backup_path" in Settings.model_fields
    env_file = tmp_path / "settings.env"
    env_file.write_text("DANG_BACKUP_PATH=\n", encoding="utf-8")
    monkeypatch.setattr(app_config, "PROJECT_ENV_FILE", env_file)
    if origin == "process":
        monkeypatch.setenv("DANG_BACKUP_PATH", "")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_identity_is_credential_independent_and_database_specific() -> None:
    module = backup_config()
    first = module.parse_target("postgresql+psycopg://u:old@127.0.0.1:5432/one")
    rotated = module.parse_target("postgresql://different:new@127.0.0.1/one")
    other = module.parse_target("postgresql+psycopg://u:new@127.0.0.1:5432/two")
    assert module.source_identity(first) == module.source_identity(rotated)
    assert module.source_identity(first) != module.source_identity(other)
    assert "old" not in repr(first)
    assert "old" not in first.model_dump_json()
    assert len(module.source_identity(first)) == 64


def test_query_preserved_and_percent_encoded_password_hidden() -> None:
    module = backup_config()
    target = module.parse_target("postgresql://u:a%3Ab%5Cc@LOCALHOST/db?sslmode=verify-full&connect_timeout=17")
    assert target.host == "localhost"
    assert target.password.get_secret_value() == "a:b\\c"
    assert target.sslmode == "verify-full"
    assert target.connect_timeout == 17
    assert "a:b" not in target.model_dump_json()


@pytest.mark.parametrize("url", [
    "mysql://u:secret-canary@host/db", "postgresql+asyncpg://u:p@host/db",
    "postgresql:///db", "postgresql://host/db", "postgresql://u:p@host/",
    "postgresql://u:p@one,two/db", "postgresql://u:p@host:0/db",
    "postgresql://u:p@host/db?sslmode=unsafe", "postgresql://u:p@host/db?connect_timeout=0",
    "postgresql://u:p@host/db?host=other", "postgresql://u:p@host/db?sslmode=require&sslmode=disable",
    "postgresql://u:p@host/host%3Dbusiness-canary%20port%3D5432", "postgresql://u:p@host%20space/db",
    "postgresql://u:p%0Asecret-canary@host/db", "postgresql://u:p@host/db\n",
])
def test_invalid_url_is_fail_closed_and_safe(url: str) -> None:
    module = backup_config()
    from app.ops.backup.contracts import BackupError
    with pytest.raises(BackupError) as caught:
        module.parse_target(url)
    assert caught.value.exit_code == 2
    assert "secret-canary" not in str(caught.value)
    assert url not in str(caught.value)


def test_options_preserve_native_paths_with_spaces() -> None:
    module = backup_config()
    options = module.backup_options(Settings(_env_file=None, backup_tool_mode="native", backup_native_bin="C:/Program Files/PostgreSQL/17/bin"))
    assert options.native_bin == Path("C:/Program Files/PostgreSQL/17/bin")


def test_empty_native_bin_explicitly_selects_path_lookup() -> None:
    module = backup_config()
    assert module.backup_options(Settings(_env_file=None, backup_native_bin="")).native_bin is None


def test_manifest_rejects_unknown_fields_and_hides_credentials() -> None:
    backup_config()
    from app.ops.backup.contracts import BackupManifest, ConnectionTarget
    with pytest.raises(ValidationError):
        BackupManifest.model_validate({"format_version": 2, "path": "../../secret"})
    target = ConnectionTarget(host="host", database="db", username="u", password="sensitive-canary")
    assert "sensitive-canary" not in repr(target)
    assert "sensitive-canary" not in target.model_dump_json()
