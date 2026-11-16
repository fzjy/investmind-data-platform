from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, time
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, ModelWrapValidatorHandler, SecretStr, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config_paths import CONFIG_ROOT, resolve_catalog_path
from app.ops.secrets import file_secret_overrides


DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"
BOOTSTRAP_SYMBOLS_ENV_NAME = "DANG_BOOTSTRAP_SYMBOLS"
EMPTY_BOOTSTRAP_SYMBOLS_ERROR = "DANG_BOOTSTRAP_SYMBOLS must not be empty"


def _case_insensitive_value(
    values: Mapping[str, str | None],
    name: str,
) -> tuple[bool, str | None]:
    found = False
    configured_value: str | None = None
    for key, value in values.items():
        if key.casefold() == name.casefold():
            found = True
            configured_value = value
    return found, configured_value


def _validate_bootstrap_symbols_input() -> None:
    found, value = _case_insensitive_value(
        os.environ,
        BOOTSTRAP_SYMBOLS_ENV_NAME,
    )
    if not found:
        found, value = _case_insensitive_value(
            dotenv_values(PROJECT_ENV_FILE, verbose=False),
            BOOTSTRAP_SYMBOLS_ENV_NAME,
        )
    if found and (value is None or not value.strip()):
        raise ValueError(EMPTY_BOOTSTRAP_SYMBOLS_ERROR)


def _validate_backup_input() -> None:
    # 保持既有 env_ignore_empty 行为，但备份路径/模式/数值显式空值不能回退。
    file_values = dotenv_values(PROJECT_ENV_FILE, verbose=False)
    for suffix in ("TOOL_MODE", "PATH", "DOCKER_IMAGE", "INTERVAL_DAYS", "KEEP_COUNT", "RETENTION_DAYS", "TIMEOUT_SECONDS"):
        name = "DANG_BACKUP_" + suffix
        found, value = _case_insensitive_value(os.environ, name)
        if not found:
            found, value = _case_insensitive_value(file_values, name)
        if found and (value is None or not value.strip()):
            raise ValueError("backup configuration must not be empty")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DANG_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    data_backend: Literal["database"] = "database"
    search_query_max_length: int = Field(default=64, ge=1, le=128)
    search_default_page_size: int = Field(default=20, ge=1, le=200)
    search_max_page_size: int = Field(default=50, ge=1, le=200)
    database_url: str | None = Field(default=None, repr=False, exclude=True)
    database_read_url: str | None = Field(default=None, repr=False, exclude=True)
    quality_max_row_drop_ratio: float = Field(default=0.2, ge=0, le=1)
    quality_max_field_drop_ratio: float = Field(default=0.2, ge=0, le=1)
    capacity_stock_count: int = Field(default=5000, ge=1, le=10000)
    capacity_trading_days: int = Field(default=750, ge=1, le=780)
    capacity_batch_size: int = Field(default=10000, ge=1, le=50000)
    capacity_api_samples: int = Field(default=10, ge=1, le=100)
    capacity_timeout_seconds: float = Field(default=1800, gt=0, le=7200)
    capacity_cleanup_timeout_seconds: float = Field(default=30, gt=0, le=120)
    capacity_build_budget_seconds: float = Field(default=300, gt=0, le=7200)
    capacity_api_budget_ms: float = Field(default=500, gt=0, le=60000)
    tushare_token: SecretStr | None = None
    tushare_mode: Literal["standard", "vip"] = "standard"
    tushare_standard_requests_per_minute: int = Field(default=30, gt=0)
    tushare_vip_requests_per_minute: int = Field(default=120, gt=0)
    tushare_page_size: int = Field(default=5000, gt=0, le=5000)
    sync_timezone: str = "Asia/Shanghai"
    sync_disclosure_lookback_days: int = Field(default=7, ge=1, le=31)
    sync_hour: int = Field(default=19, ge=0, le=23)
    sync_minute: int = Field(default=0, ge=0, le=59)
    sync_universe: Literal["representative-50", "custom", "all"] = "representative-50"
    sync_universe_file: Path | None = None
    backfill_as_of: date | None = None
    stock_update_as_of: date | None = None
    stock_update_max_requests_per_run: int = Field(default=512, ge=1, le=10000)
    stock_update_calendar_lookahead_days: int = Field(default=31, ge=1, le=366)
    scheduler_daily_enabled: bool = True
    scheduler_announcements_enabled: bool | None = None
    routine_stocks_enabled: bool = True
    routine_macro_enabled: bool = True
    routine_market_enabled: bool = False
    routine_market_pulse_enabled: bool = False
    routine_region_enabled: bool = False
    routine_etf_heatmap_enabled: bool = False
    routine_commodities_enabled: bool = False
    backfill_market_years: int = Field(default=3, ge=1, le=10)
    backfill_financial_start: date | None = None
    backfill_batch_size: int = Field(default=10, ge=1, le=100)
    sync_failure_alert_threshold: int = Field(default=3, ge=1, le=100)
    sync_catchup_max_days: int = Field(default=10, ge=1, le=250)
    scheduler_heartbeat_seconds: int = Field(default=60, ge=10, le=3600)
    scheduler_heartbeat_timeout_seconds: int = Field(default=180, ge=11, le=86400)
    sync_freshness_interval_seconds: int = Field(default=300, ge=30, le=86400)
    macro_sync_enabled: bool = True
    macro_sync_day_of_week: Literal[
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"
    ] = "mon"
    macro_sync_time: time = time(9, 0)
    macro_sync_catchup_on_start: bool = True
    macro_history_years: int = Field(default=10, ge=1, le=30)
    macro_enterprise_universe: Literal["representative-50", "custom", "all"] = "representative-50"
    macro_enterprise_universe_file: Path | None = None
    macro_enterprise_min_coverage: int = Field(default=80, ge=1, le=100)
    macro_enterprise_min_samples: int = Field(default=5, ge=1, le=2**53-1)
    macro_enterprise_max_quarters: int = Field(default=40, ge=1, le=40)
    macro_enterprise_supplement_quarters: int = Field(default=8, ge=1, le=8)
    macro_catchup_request_limit: int = Field(default=100, ge=1, le=1000)
    macro_us_rates_request_limit: int = Field(default=4, ge=2, le=100)
    macro_nbs_request_limit: int = Field(default=64, ge=2, le=100)
    macro_nbs_lookback_months: int = Field(default=3, ge=1, le=18)
    macro_tushare_request_limit: int = Field(default=6, ge=6, le=100)
    macro_fred_request_limit: int = Field(default=16, ge=12, le=100)
    macro_backfill_request_limit: int = Field(default=12, ge=2, le=100)
    macro_backfill_end: date | None = None
    macro_backfill_releases_file: Path | None = None
    fred_api_key: SecretStr | None = None
    bootstrap_symbols: str = "600519.SH"
    bootstrap_as_of: date | None = None
    bootstrap_force: bool = False
    external_import_path: Path = Path("./imports")
    news_config_path: Path = CONFIG_ROOT / "news/news-collector.json"
    backup_tool_mode: Literal["native", "docker"] = "docker"
    backup_path: str = "./backend/backups"
    backup_native_bin: str | None = None
    backup_docker_image: str = "postgres:17-alpine"
    backup_interval_days: int = Field(default=7, ge=1, le=365)
    backup_keep_count: int = Field(default=8, ge=2, le=100)
    backup_retention_days: int = Field(default=56, ge=1, le=3650)
    backup_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    cors_origins: tuple[str, ...] = Field(
        default=DEFAULT_CORS_ORIGINS,
        validation_alias=AliasChoices("BACKEND_CORS_ORIGINS", "CORS_ORIGINS"),
    )

    @field_validator("news_config_path", "sync_universe_file", "macro_enterprise_universe_file", "macro_backfill_releases_file")
    @classmethod
    def resolve_configured_catalog(cls, value: Path | None, info: ValidationInfo) -> Path | None:
        if value is None:
            return None
        catalogs = {
            "news_config_path": ("news/news-collector.json", "news-collector.json"),
            "sync_universe_file": ("stocks/universes/representative-50.json", "universes/representative-50.json"),
            "macro_enterprise_universe_file": ("stocks/universes/representative-50.json", "universes/representative-50.json"),
            "macro_backfill_releases_file": ("macro/nbs-history-releases.json", "nbs-history-releases.json"),
        }
        return resolve_catalog_path(value, *catalogs[info.field_name])

    @field_validator("backup_path", "backup_native_bin", "backup_docker_image")
    @classmethod
    def validate_backup_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value in (None, "") and info.field_name == "backup_native_bin":
            return None
        if not value or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid backup configuration")
        if info.field_name == "backup_docker_image":
            tag = value.rsplit(":", 1)
            if len(tag) != 2 or not tag[1] or tag[1].lower() == "latest" or any(char.isspace() for char in value):
                raise ValueError("backup image must have an explicit non-latest tag")
        return value

    @model_validator(mode="after")
    def validate_database_configuration(self) -> "Settings":
        if (self.macro_enterprise_universe == "custom") != (self.macro_enterprise_universe_file is not None):
            raise ValueError("enterprise universe file is required for custom and forbidden for other profiles")
        if self.scheduler_heartbeat_timeout_seconds <= self.scheduler_heartbeat_seconds:
            raise ValueError("heartbeat timeout must exceed heartbeat interval")
        if (
            self.backfill_as_of is not None
            and self.backfill_financial_start is not None
            and self.backfill_financial_start > self.backfill_as_of
        ):
            raise ValueError("backfill financial start must not follow as-of")
        if not self.database_read_url:
            self.database_read_url = self.database_url
        return self

    @model_validator(mode="wrap")
    @classmethod
    def hide_validation_inputs(
        cls, value: object, handler: ModelWrapValidatorHandler["Settings"],
    ) -> "Settings":
        try:
            return handler(value)
        except ValidationError as error:
            # hide_input_in_errors 仅保护文本；errors()/json() 也不能携带原始密钥。
            details = cast(list[InitErrorDetails], error.errors(include_input=False, include_url=False))
            raise ValidationError.from_exception_data(error.title, details, hide_input=True) from None

    @classmethod
    def from_env(cls) -> "Settings":
        _validate_bootstrap_symbols_input()
        _validate_backup_input()
        overrides = file_secret_overrides(dotenv_values(PROJECT_ENV_FILE, verbose=False))
        return cls(_env_file=PROJECT_ENV_FILE, **overrides)


def configured_tushare_token() -> str:
    # _FILE 配置必须先解析；坏文件不能被旧明文或下方兼容分支吞掉。
    overrides = file_secret_overrides({}, ("tushare_token",))
    if "tushare_token" in overrides:
        return overrides["tushare_token"]
    process_token = os.environ.get("DANG_TUSHARE_TOKEN")
    try:
        file_values = dotenv_values(PROJECT_ENV_FILE, verbose=False)
    except Exception:
        return process_token or ""
    overrides = file_secret_overrides(file_values, ("tushare_token",))
    if "tushare_token" in overrides:
        return overrides["tushare_token"]
    return process_token or file_values.get("DANG_TUSHARE_TOKEN") or ""


def __getattr__(name: str) -> object:
    if name == "settings":
        return Settings.from_env()
    raise AttributeError(name)
