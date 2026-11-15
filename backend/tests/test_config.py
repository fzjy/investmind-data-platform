from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.config as config_module
from app.config import Settings


def test_settings_defaults_to_database_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANG_DATA_BACKEND", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_MODE", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_PAGE_SIZE", raising=False)
    monkeypatch.delenv("DANG_SYNC_DISCLOSURE_LOOKBACK_DAYS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.data_backend == "database"
    assert settings.tushare_mode == "standard"
    assert settings.tushare_standard_requests_per_minute == 30
    assert settings.tushare_vip_requests_per_minute == 120
    assert settings.tushare_page_size == 5000
    assert settings.sync_disclosure_lookback_days == 7


def test_settings_rejects_unknown_tushare_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANG_TUSHARE_MODE", "auto")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_macro_settings_defaults_are_weekly_and_ten_years() -> None:
    settings = Settings(_env_file=None)

    assert settings.macro_sync_enabled is True
    assert settings.macro_sync_day_of_week == "mon"
    assert settings.macro_sync_time == time(9, 0)
    assert settings.macro_sync_catchup_on_start is True
    assert settings.macro_history_years == 10
    assert settings.fred_api_key is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("macro_sync_day_of_week", "weekday"),
        ("macro_sync_time", "25:00"),
        ("macro_history_years", 0),
        ("macro_history_years", 31),
    ],
)
def test_macro_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_fred_api_key_uses_secret_representation() -> None:
    settings = Settings(_env_file=None, fred_api_key="fred-test-secret")

    assert "fred-test-secret" not in repr(settings.fred_api_key)
    assert str(settings.fred_api_key) == "**********"


def test_settings_allow_planning_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    monkeypatch.delenv("DANG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DANG_DATABASE_READ_URL", raising=False)

    assert Settings(_env_file=None).database_url is None


def test_settings_reject_removed_fixture_mode(monkeypatch):
    monkeypatch.setenv("DANG_DATA_BACKEND", "fixture")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_validation_errors_hide_input_values() -> None:
    invalid_value = "not-a-valid-secret-scalar"

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, bootstrap_force=invalid_value)

    assert invalid_value not in str(exc_info.value)


def test_settings_load_project_env_file_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_DATA_BACKEND=database\n"
        "DANG_DATABASE_URL=sqlite+pysqlite:///configured.db\n"
        "DANG_SYNC_TIMEZONE=UTC\n",
        encoding="utf-8",
    )
    nested_directory = tmp_path / "nested"
    nested_directory.mkdir()
    monkeypatch.chdir(nested_directory)
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file, raising=False)
    monkeypatch.delenv("DANG_DATA_BACKEND", raising=False)
    monkeypatch.delenv("DANG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DANG_DATABASE_READ_URL", raising=False)
    monkeypatch.delenv("DANG_SYNC_TIMEZONE", raising=False)

    settings = Settings.from_env()

    assert settings.data_backend == "database"
    assert settings.database_url == "sqlite+pysqlite:///configured.db"
    assert settings.database_read_url == settings.database_url
    assert settings.sync_timezone == "UTC"


def test_settings_ignore_empty_optional_values_in_project_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_DATA_BACKEND=database\n"
        "DANG_DATABASE_URL=\n"
        "DANG_DATABASE_READ_URL=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.delenv("DANG_DATA_BACKEND", raising=False)
    monkeypatch.delenv("DANG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DANG_DATABASE_READ_URL", raising=False)

    settings = Settings.from_env()

    assert settings.database_url is None
    assert settings.database_read_url is None


def test_bootstrap_symbols_use_default_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DANG_DATA_BACKEND=database\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.delenv("DANG_BOOTSTRAP_SYMBOLS", raising=False)

    settings = Settings.from_env()

    assert settings.bootstrap_symbols == "600519.SH"


def test_process_environment_rejects_explicit_empty_bootstrap_symbols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_BOOTSTRAP_SYMBOLS=000001.SZ\n"
        "DANG_TUSHARE_TOKEN=fixture-token-must-not-appear\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.setenv("DANG_BOOTSTRAP_SYMBOLS", "")

    with pytest.raises(ValueError) as exc_info:
        Settings.from_env()

    assert str(exc_info.value) == "DANG_BOOTSTRAP_SYMBOLS must not be empty"
    assert "fixture-token-must-not-appear" not in str(exc_info.value)


def test_project_env_rejects_explicit_empty_bootstrap_symbols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_BOOTSTRAP_SYMBOLS=\n"
        "DANG_DATABASE_READ_URL=postgresql://user:password@database/app\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.delenv("DANG_BOOTSTRAP_SYMBOLS", raising=False)

    with pytest.raises(ValueError) as exc_info:
        Settings.from_env()

    assert str(exc_info.value) == "DANG_BOOTSTRAP_SYMBOLS must not be empty"
    assert "password" not in str(exc_info.value)


def test_nonempty_process_bootstrap_symbols_override_empty_project_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DANG_BOOTSTRAP_SYMBOLS=\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.setenv("DANG_BOOTSTRAP_SYMBOLS", "000001.SZ")

    settings = Settings.from_env()

    assert settings.bootstrap_symbols == "000001.SZ"


def test_project_env_example_is_valid_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_example = project_root / ".env.example"
    for name in (
        "DANG_DATA_BACKEND",
        "DANG_DATABASE_URL",
        "DANG_DATABASE_READ_URL",
        "DANG_TUSHARE_TOKEN",
        "DANG_TUSHARE_MODE",
        "DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE",
        "DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE",
        "DANG_TUSHARE_PAGE_SIZE",
        "DANG_SYNC_TIMEZONE",
        "DANG_SYNC_DISCLOSURE_LOOKBACK_DAYS",
        "DANG_EXTERNAL_IMPORT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=env_example)

    assert env_example.is_file()
    assert settings.data_backend == "database"
    assert settings.database_url is None
    assert settings.tushare_token is None
    assert settings.external_import_path == Path("./backend/imports")
    assert settings.tushare_standard_requests_per_minute == 30
    assert settings.tushare_vip_requests_per_minute == 120
    assert settings.tushare_page_size == 5000
    assert settings.sync_disclosure_lookback_days == 7


@pytest.mark.parametrize("value", (0, 32))
def test_settings_reject_invalid_disclosure_lookback(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sync_disclosure_lookback_days=value)


def test_process_environment_overrides_project_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DANG_DATA_BACKEND=database\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    monkeypatch.setenv("DANG_DATABASE_URL", "sqlite+pysqlite:///override.db")

    settings = Settings.from_env()

    assert settings.data_backend == "database"
    assert settings.database_url == "sqlite+pysqlite:///override.db"


def test_settings_loads_tushare_pagination_and_rate_limits_from_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE=45\n"
        "DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE=180\n"
        "DANG_TUSHARE_PAGE_SIZE=4000\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.delenv("DANG_TUSHARE_PAGE_SIZE", raising=False)

    settings = Settings(_env_file=env_file)

    assert settings.tushare_standard_requests_per_minute == 45
    assert settings.tushare_vip_requests_per_minute == 180
    assert settings.tushare_page_size == 4000


def test_process_environment_overrides_tushare_pagination_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE=45\n"
        "DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE=180\n"
        "DANG_TUSHARE_PAGE_SIZE=4000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setenv("DANG_TUSHARE_VIP_REQUESTS_PER_MINUTE", "240")
    monkeypatch.setenv("DANG_TUSHARE_PAGE_SIZE", "3500")

    settings = Settings(_env_file=env_file)

    assert settings.tushare_standard_requests_per_minute == 60
    assert settings.tushare_vip_requests_per_minute == 240
    assert settings.tushare_page_size == 3500


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tushare_standard_requests_per_minute", 0),
        ("tushare_vip_requests_per_minute", 0),
        ("tushare_page_size", 0),
        ("tushare_page_size", 5001),
    ],
)
def test_settings_reject_invalid_tushare_rate_limit_values(
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: value})


def test_universe_backfill_settings_have_central_defaults() -> None:
    settings = Settings()

    assert settings.backfill_as_of is None
    assert settings.backfill_market_years == 3
    assert settings.backfill_financial_start is None
    assert settings.backfill_batch_size == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backfill_market_years", 0),
        ("backfill_market_years", 11),
        ("backfill_batch_size", 0),
        ("backfill_batch_size", 101),
    ],
)
def test_universe_backfill_numeric_settings_reject_out_of_range(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_universe_backfill_financial_start_cannot_follow_as_of() -> None:
    with pytest.raises(ValidationError, match="financial start"):
        Settings(
            backfill_as_of=date(2026, 9, 12),
            backfill_financial_start=date(2026, 9, 13),
        )


def test_scheduler_settings_have_personal_machine_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.sync_hour == 19
    assert settings.sync_minute == 0
    assert settings.sync_universe == "representative-50"
    assert settings.sync_universe_file is None
    assert settings.sync_failure_alert_threshold == 3
    assert settings.sync_catchup_max_days == 10
    assert settings.scheduler_heartbeat_seconds == 60
    assert settings.scheduler_heartbeat_timeout_seconds == 180
    assert settings.sync_freshness_interval_seconds == 300


@pytest.mark.parametrize(
    ("field", "value"),
    [("sync_hour", 24), ("sync_minute", 60), ("sync_catchup_max_days", 0)],
)
def test_scheduler_settings_reject_out_of_range_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_scheduler_timeout_must_exceed_heartbeat_interval() -> None:
    with pytest.raises(ValidationError, match="heartbeat timeout"):
        Settings(
            _env_file=None,
            scheduler_heartbeat_seconds=60,
            scheduler_heartbeat_timeout_seconds=60,
        )

def test_legacy_news_catalog_and_custom_path(tmp_path):
    from app.config_paths import CONFIG_ROOT

    for path in ("backend/config/news-collector.json", "config/news/news-collector.json"):
        settings = Settings(news_config_path=path)
        assert settings.news_config_path == CONFIG_ROOT / "news/news-collector.json"
        assert settings.news_config_path.is_file()
    custom = tmp_path / "own-news.json"
    assert Settings(news_config_path=custom).news_config_path == custom
