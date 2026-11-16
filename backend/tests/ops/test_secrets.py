from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import app.config as config
from app.config import Settings, configured_tushare_token


SECRET_FIELDS = (
    ("database_url", "postgresql://user:write-canary@localhost/write"),
    ("database_read_url", "postgresql://user:read-canary@localhost/read"),
    ("tushare_token", "tushare-file-canary"),
    ("fred_api_key", "fred-file-canary"),
)


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("DANG_"):
            monkeypatch.delenv(name)
    env_file = tmp_path / "root.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", env_file)


def actual_value(settings: Settings, field: str) -> str | None:
    value = getattr(settings, field)
    return value.get_secret_value() if isinstance(value, SecretStr) else value


@pytest.mark.parametrize(("field", "secret"), SECRET_FIELDS)
@pytest.mark.parametrize("source", ("process", "dotenv"))
def test_settings_consumes_mounted_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, secret: str, source: str,
) -> None:
    # 忽略任一 _FILE 输入会让真实配置消费者拿不到独立给定的值。
    secret_file = tmp_path / "mounted secret"
    secret_file.write_bytes((secret + "\r\n").encode())
    name = "DANG_" + field.upper() + "_FILE"
    if source == "process":
        monkeypatch.setenv(name, str(secret_file))
    else:
        config.PROJECT_ENV_FILE.write_text(f'{name}="{secret_file.as_posix()}"\n', encoding="utf-8")

    settings = Settings.from_env()

    assert actual_value(settings, field) == secret
    if field == "database_url":
        assert settings.database_read_url == secret
    if field == "tushare_token":
        assert configured_tushare_token() == "tushare-file-canary"


@pytest.mark.parametrize(("field", "secret"), SECRET_FIELDS)
@pytest.mark.parametrize("scenario", ("file_over_process_plain", "file_over_dotenv_plain", "process_file_over_dotenv_file"))
def test_explicit_files_win_without_plaintext_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, secret: str, scenario: str,
) -> None:
    name = "DANG_" + field.upper()
    chosen = tmp_path / "chosen"
    chosen.write_text(secret, encoding="utf-8")
    if scenario == "file_over_process_plain":
        config.PROJECT_ENV_FILE.write_text(f'{name}_FILE="{chosen.as_posix()}"\n', encoding="utf-8")
        monkeypatch.setenv(name, "process-plaintext-canary")
    elif scenario == "file_over_dotenv_plain":
        config.PROJECT_ENV_FILE.write_text(f"{name}=dotenv-plaintext-canary\n", encoding="utf-8")
        monkeypatch.setenv(name + "_FILE", str(chosen))
    else:
        config.PROJECT_ENV_FILE.write_text(f'{name}_FILE="{(tmp_path / "missing").as_posix()}"\n', encoding="utf-8")
        monkeypatch.setenv(name + "_FILE", str(chosen))

    assert actual_value(Settings.from_env(), field) == secret
    if field == "tushare_token":
        assert configured_tushare_token() == secret


@pytest.mark.parametrize("consumer", (Settings.from_env, configured_tushare_token))
@pytest.mark.parametrize("bad_input", (
    "missing", "directory", "empty", "blank", "invalid_utf8", "oversize", "multiline",
    "nul_content", "empty_path", "relative_path", "nul_path", "newline_path",
))
def test_bad_file_fails_safely_even_when_plaintext_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: Callable[[], object], bad_input: str,
) -> None:
    secret_file = tmp_path / "private-path-canary"
    payloads = {
        "empty": b"", "blank": b"   \r\n", "invalid_utf8": b"\xffvalue-canary",
        "oversize": b"x" * (65536 + 1), "multiline": b"value-canary\n\n",
        "nul_content": b"value-canary\x00",
    }
    if bad_input == "directory":
        secret_file.mkdir()
    elif bad_input in payloads:
        secret_file.write_bytes(payloads[bad_input])
    path = {
        "empty_path": "", "relative_path": "private-path-canary",
        "nul_path": "private-path-canary\x00", "newline_path": str(secret_file) + "\n",
    }.get(bad_input, str(secret_file))
    if bad_input == "nul_path":
        # OS 环境变量拒绝 NUL；dotenv 是可以实际传入该非法路径的配置边界。
        config.PROJECT_ENV_FILE.write_text(f"DANG_TUSHARE_TOKEN_FILE={path}\n", encoding="utf-8")
    else:
        monkeypatch.setenv("DANG_TUSHARE_TOKEN_FILE", path)
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", "plaintext-canary")

    with pytest.raises(ValueError) as caught:
        consumer()

    rendered = str(caught.value) + repr(caught.value)
    for canary in ("private-path-canary", "value-canary", "plaintext-canary", str(tmp_path)):
        assert canary not in rendered
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("consumer", (Settings.from_env, configured_tushare_token))
def test_permission_error_is_safe_without_changing_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: Callable[[], object],
) -> None:
    secret_file = tmp_path / "permission-path-canary"
    secret_file.write_text("value-canary", encoding="utf-8")
    monkeypatch.setenv("DANG_TUSHARE_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", "plaintext-canary")
    original_open = Path.open

    def controlled_open(path: Path, *args: object, **kwargs: object):
        if path == secret_file:
            raise PermissionError(f"permission-path-canary: {secret_file}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", controlled_open)
    with pytest.raises(ValueError) as caught:
        consumer()
    assert "permission-path-canary" not in str(caught.value)
    assert "plaintext-canary" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("suffix", (b"", b"\n", b"\r\n"))
def test_secret_spaces_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: bytes) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b"  password with spaces  " + suffix)
    monkeypatch.setenv("DANG_TUSHARE_TOKEN_FILE", str(secret_file))
    assert configured_tushare_token() == "  password with spaces  "
    assert Settings.from_env().tushare_token.get_secret_value() == "  password with spaces  "


def test_file_size_boundary_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b"x" * 65535 + b"\n")
    monkeypatch.setenv("DANG_TUSHARE_TOKEN_FILE", str(secret_file))
    assert configured_tushare_token() == "x" * 65535


def test_case_insensitive_file_environment_is_consumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("case-canary", encoding="utf-8")
    monkeypatch.setenv("dang_tushare_token_file", str(secret_file))
    assert Settings.from_env().tushare_token.get_secret_value() == "case-canary"
    assert configured_tushare_token() == "case-canary"


@pytest.mark.parametrize("consumer", (Settings.from_env, configured_tushare_token))
def test_empty_dotenv_file_declaration_is_not_ignored(monkeypatch: pytest.MonkeyPatch, consumer: Callable[[], object]) -> None:
    config.PROJECT_ENV_FILE.write_text("DANG_TUSHARE_TOKEN_FILE=\nDANG_TUSHARE_TOKEN=plain-canary\n", encoding="utf-8")
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", "process-canary")
    with pytest.raises(ValueError):
        consumer()


@pytest.mark.parametrize(("field", "secret"), SECRET_FIELDS)
def test_no_file_keeps_process_plaintext_priority(monkeypatch: pytest.MonkeyPatch, field: str, secret: str) -> None:
    name = "DANG_" + field.upper()
    config.PROJECT_ENV_FILE.write_text(f"{name}=dotenv-canary\n", encoding="utf-8")
    monkeypatch.setenv(name, secret)
    assert actual_value(Settings.from_env(), field) == secret
    if field == "tushare_token":
        assert configured_tushare_token() == secret


def test_constructor_still_accepts_database_strings_and_secret_tokens() -> None:
    settings = Settings(_env_file=None, data_backend="database", database_url="postgresql+psycopg://controlled.invalid/test", tushare_token="local-token")
    assert settings.database_url == "postgresql+psycopg://controlled.invalid/test"
    assert settings.database_read_url == "postgresql+psycopg://controlled.invalid/test"
    assert settings.tushare_token.get_secret_value() == "local-token"


@pytest.mark.parametrize(("process_token", "expected"), ((None, ""), ("process-canary", "process-canary")))
def test_no_file_keeps_token_behavior_when_dotenv_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, process_token: str | None, expected: str,
) -> None:
    import builtins

    if process_token is not None:
        monkeypatch.setenv("DANG_TUSHARE_TOKEN", process_token)
    original_open = builtins.open

    def denied_dotenv(file: object, *args: object, **kwargs: object):
        if file == config.PROJECT_ENV_FILE:
            raise PermissionError("dotenv-path-canary")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied_dotenv)
    assert configured_tushare_token() == expected


@pytest.mark.parametrize(("field", "secret"), SECRET_FIELDS[:2])
def test_database_urls_are_not_serialized_or_represented(field: str, secret: str) -> None:
    settings = Settings(_env_file=None, **{field: secret})
    assert actual_value(settings, field) == secret
    for rendered in (repr(settings), str(settings), repr(settings.model_dump()), settings.model_dump_json()):
        assert "canary" not in rendered


def test_model_validation_error_does_not_reveal_database_password() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, database_url="postgresql://user:password-canary@host/db", scheduler_heartbeat_timeout_seconds=60)
    assert "password-canary" not in str(caught.value)
    assert "password-canary" not in repr(caught.value)
    assert "password-canary" not in repr(caught.value.errors())
    assert "password-canary" not in caught.value.json()


def test_log_redaction_inputs_use_resolved_files_not_stale_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import importlib.util

    assert importlib.util.find_spec("app.ops.secrets") is not None, "mounted secret resolver is missing"
    secrets = importlib.import_module("app.ops.secrets")
    for field, value in (
        ("database_url", "postgresql://user:pw%3Acanary@localhost/write"),
        ("tushare_token", "resolved-token-canary"),
        ("fred_api_key", "resolved-key-canary"),
    ):
        secret_file = tmp_path / field
        secret_file.write_text(value, encoding="utf-8")
        monkeypatch.setenv("DANG_" + field.upper() + "_FILE", str(secret_file))
        monkeypatch.setenv("DANG_" + field.upper(), "stale-plaintext-canary")

    values = secrets.resolved_secret_values(Settings.from_env())

    assert "resolved-token-canary" in values
    assert "resolved-key-canary" in values
    assert "postgresql://user:pw%3Acanary@localhost/write" in values
    assert "pw%3Acanary" in values
    assert "pw:canary" in values
    assert "stale-plaintext-canary" not in values
    assert "**********" not in values


@pytest.mark.parametrize("field", ("quality_max_row_drop_ratio", "quality_max_field_drop_ratio"))
def test_quality_drop_limits_default_to_twenty_percent(field: str) -> None:
    assert getattr(Settings(_env_file=None), field, None) == 0.2


@pytest.mark.parametrize("field", ("quality_max_row_drop_ratio", "quality_max_field_drop_ratio"))
@pytest.mark.parametrize("value", (-0.01, 1.01, float("nan"), float("inf"), float("-inf")))
def test_quality_drop_limits_reject_nonfinite_or_out_of_range_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", ("quality_max_row_drop_ratio", "quality_max_field_drop_ratio"))
@pytest.mark.parametrize("value", (0.0, 1.0))
def test_quality_drop_limits_allow_explicit_boundary_policies(field: str, value: float) -> None:
    assert getattr(Settings(_env_file=None, **{field: value}), field, None) == value


def test_quality_drop_limits_load_from_root_configuration() -> None:
    config.PROJECT_ENV_FILE.write_text(
        "DANG_QUALITY_MAX_ROW_DROP_RATIO=0.125\nDANG_QUALITY_MAX_FIELD_DROP_RATIO=0.375\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()
    assert getattr(settings, "quality_max_row_drop_ratio", None) == 0.125
    assert getattr(settings, "quality_max_field_drop_ratio", None) == 0.375
