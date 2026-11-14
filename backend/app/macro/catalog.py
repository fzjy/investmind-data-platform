from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError, model_validator


MacroFrequency = Literal["daily", "monthly", "quarterly"]
MacroRegion = Literal["CN", "US"]
MacroCategory = Literal["growth", "inflation", "money_credit", "labor", "rates_fx"]
MacroTransform = Literal["identity", "pc1", "chg", "qoq_annualized"]
MacroAccessMode = Literal["api", "public_download", "public_page"]
MacroSeasonalAdjustment = Literal[
    "seasonally_adjusted",
    "not_adjusted",
    "unknown",
]

DEFAULT_MACRO_CATALOG_FILE = (
    Path(__file__).resolve().parents[2] / "config" / "macro-series.json"
)
EXPECTED_SERIES_IDS = (
    "cn.gdp.real_yoy",
    "cn.cpi.yoy",
    "cn.cpi.core_yoy",
    "cn.ppi.yoy",
    "cn.pmi.manufacturing",
    "cn.industry.value_added_yoy",
    "cn.money.m1_yoy",
    "cn.money.m2_yoy",
    "cn.credit.tsf_stock_yoy",
    "cn.rate.dr007",
    "cn.rate.govbond_10y",
    "cn.fx.usdcny",
    "us.gdp.real_qoq_annualized",
    "us.cpi.yoy",
    "us.cpi.core_yoy",
    "us.pce.core_yoy",
    "us.labor.unemployment_rate",
    "us.labor.nonfarm_payroll_change",
    "us.rate.effective_fed_funds",
    "us.rate.treasury_10y",
)
SUPPORTED_TRANSFORMS = frozenset({"identity", "pc1", "chg", "qoq_annualized"})
FORBIDDEN_SECRET_KEY_PARTS = frozenset(
    {"apikey", "token", "password", "databaseurl"}
)

# 仅兼容已找回原文件、逐字节规范化后相同的 v7 历史身份。
# 完整证据与混合换行位置见 docs/research/nbs-catalog-identity-2026-10-05.md。
# 未知旧 hash 必须另行提供原文件证据，不能按版本号自动信任。
_VERIFIED_LEGACY_CATALOG_HASHES = {
    "85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31":
        "0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a",
    "7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde":
        "0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a",
}


def catalog_hashes_match(recorded_hash: object, expected_hash: str) -> bool:
    """只读比较；不改写记录，也不兼容未知格式或内容变化。"""
    if not isinstance(recorded_hash, str):
        return False
    return _VERIFIED_LEGACY_CATALOG_HASHES.get(recorded_hash, recorded_hash) == (
        _VERIFIED_LEGACY_CATALOG_HASHES.get(expected_hash, expected_hash)
    )


class MacroCatalogError(ValueError):
    """Raised when a macro catalog cannot be safely used."""


class MacroOverlapDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    daily_days: int = Field(alias="dailyDays", ge=1)
    monthly_months: int = Field(alias="monthlyMonths", ge=1)
    quarterly_quarters: int = Field(alias="quarterlyQuarters", ge=1)


class MacroDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    request_timeout_seconds: int = Field(alias="requestTimeoutSeconds", ge=1)
    max_attempts: int = Field(alias="maxAttempts", ge=1)
    retry_backoff_seconds: tuple[int, ...] = Field(
        alias="retryBackoffSeconds",
        min_length=1,
    )
    freshness_days: dict[MacroFrequency, int] = Field(alias="freshnessDays")
    overlap: MacroOverlapDefaults

    @model_validator(mode="after")
    def validate_freshness_days(self) -> Self:
        required = {"daily", "monthly", "quarterly"}
        if set(self.freshness_days) != required or any(
            days < 1 for days in self.freshness_days.values()
        ):
            raise ValueError("freshnessDays must define positive daily, monthly, quarterly values")
        return self


class MacroSourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    provider: str = Field(min_length=1)
    priority: int = Field(ge=1, strict=True)
    series: str | None = Field(default=None, min_length=1)
    api: str | None = Field(default=None, min_length=1)
    table: str | None = Field(default=None, min_length=1)
    field: str = Field(min_length=1)
    transform: MacroTransform = "identity"
    source_url: AnyHttpUrl = Field(alias="sourceUrl")
    terms_url: AnyHttpUrl | None = Field(default=None, alias="termsUrl")
    access_mode: MacroAccessMode = Field(alias="accessMode")
    display_allowed: bool = Field(alias="displayAllowed")
    params: dict[str, str] = Field(default_factory=dict)
    response_fields: tuple[str, ...] = Field(default=(), alias="responseFields")

    @model_validator(mode="after")
    def validate_provider_locator(self) -> Self:
        if self.series is None and self.api is None:
            raise ValueError("macro source requires series or api")
        return self


class MacroSeriesDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.]+$")
    name: str = Field(min_length=1)
    region: MacroRegion
    category: MacroCategory
    frequency: MacroFrequency
    unit: str = Field(min_length=1)
    seasonal_adjustment: MacroSeasonalAdjustment = Field(alias="seasonalAdjustment")
    display_precision: int = Field(alias="displayPrecision", ge=0, le=6)
    freshness_days: int = Field(alias="freshnessDays", ge=1)
    sources: tuple[MacroSourceDefinition, ...]


class MacroSeriesCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    catalog_version: str = Field(alias="catalogVersion", min_length=1)
    defaults: MacroDefaults
    series: tuple[MacroSeriesDefinition, ...]
    catalog_hash: str

    @classmethod
    def load(cls, path: Path) -> "MacroSeriesCatalog":
        try:
            catalog_bytes = path.read_bytes()
        except OSError as exc:
            raise MacroCatalogError(f"cannot read macro catalog: {path}") from exc
        try:
            raw_payload: object = json.loads(catalog_bytes)
        except json.JSONDecodeError as exc:
            raise MacroCatalogError("macro catalog is not valid JSON") from exc

        cls._validate_raw_payload(raw_payload)
        if not isinstance(raw_payload, Mapping):
            raise MacroCatalogError("macro catalog must be a JSON object")
        try:
            return cls.model_validate(
                {**raw_payload, "catalog_hash": sha256(catalog_bytes.replace(b"\r\n", b"\n")).hexdigest()}
            )
        except ValidationError as exc:
            raise MacroCatalogError("macro catalog is invalid") from exc

    def require(self, series_id: str) -> MacroSeriesDefinition:
        for definition in self.series:
            if definition.id == series_id:
                return definition
        raise MacroCatalogError(f"macro catalog series is not registered: {series_id}")

    @classmethod
    def _validate_raw_payload(cls, raw_payload: object) -> None:
        forbidden_key = _find_forbidden_secret_key(raw_payload)
        if forbidden_key is not None:
            raise MacroCatalogError(
                f"macro catalog contains forbidden secret-shaped key: {forbidden_key}"
            )
        unsupported_transform = _find_unsupported_transform(raw_payload)
        if unsupported_transform is not None:
            raise MacroCatalogError(
                f"macro catalog has unsupported transform: {unsupported_transform}"
            )
        if not isinstance(raw_payload, Mapping):
            return
        raw_series = raw_payload.get("series")
        if not isinstance(raw_series, list):
            return

        ids: list[str] = []
        for raw_definition in raw_series:
            if not isinstance(raw_definition, Mapping):
                continue
            raw_id = raw_definition.get("id")
            if not isinstance(raw_id, str):
                continue
            if raw_id in ids:
                raise MacroCatalogError(
                    f"macro catalog contains duplicate series id: {raw_id}"
                )
            ids.append(raw_id)
            cls._validate_source_priorities(raw_id, raw_definition.get("sources"))

        if tuple(ids) != EXPECTED_SERIES_IDS:
            raise MacroCatalogError("macro catalog must contain the fixed twenty series in order")

    @staticmethod
    def _validate_source_priorities(series_id: str, raw_sources: object) -> None:
        if not isinstance(raw_sources, list):
            return
        priorities: list[int] = []
        for raw_source in raw_sources:
            if not isinstance(raw_source, Mapping):
                continue
            priority = raw_source.get("priority")
            if not isinstance(priority, int):
                continue
            if priority in priorities:
                raise MacroCatalogError(
                    f"macro series sources have duplicate priority: {series_id}"
                )
            priorities.append(priority)
        if priorities and tuple(sorted(priorities)) != tuple(range(1, len(priorities) + 1)):
            raise MacroCatalogError(
                f"macro series source priorities must start at one: {series_id}"
            )


def _find_forbidden_secret_key(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if isinstance(key, str) and _is_forbidden_secret_key(key):
                return key
            nested_key = _find_forbidden_secret_key(nested_value)
            if nested_key is not None:
                return nested_key
    elif isinstance(value, list):
        for nested_value in value:
            nested_key = _find_forbidden_secret_key(nested_value)
            if nested_key is not None:
                return nested_key
    return None


def _is_forbidden_secret_key(key: str) -> bool:
    normalized_key = "".join(character for character in key.casefold() if character.isalnum())
    return any(part in normalized_key for part in FORBIDDEN_SECRET_KEY_PARTS)


def _find_unsupported_transform(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if key == "transform" and (
                not isinstance(nested_value, str) or nested_value not in SUPPORTED_TRANSFORMS
            ):
                return str(nested_value)
            unsupported = _find_unsupported_transform(nested_value)
            if unsupported is not None:
                return unsupported
    elif isinstance(value, list):
        for nested_value in value:
            unsupported = _find_unsupported_transform(nested_value)
            if unsupported is not None:
                return unsupported
    return None
