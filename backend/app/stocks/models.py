from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator
from app.stocks.rea_fields import REA_THRESHOLD_ROWS
from app.stocks.bank import BankRequest


class Categories(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    major_industry: list[str] = Field(alias="majorIndustry")
    sub_industry: list[str] = Field(alias="subIndustry")
    concept: list[str]
    region: list[str]


Thresholds = create_model("Thresholds", __config__=ConfigDict(extra="forbid", allow_inf_nan=False), **{
    attribute: (float, Field(default=default, alias=key))
    for key, attribute, direction, default in REA_THRESHOLD_ROWS
})

BoardId = Literal["sh-main", "sz-main", "star", "chinext", "bse", "hk"]
ExclusionKey = Literal[
    "loss",
    "st",
    "inventoryRisk",
    "lowRealMoney",
    "netDebt",
    "performanceDecline",
    "noDividend",
    "revenueDecline",
    "cashflowDecline",
]
SortField = Literal[
    "dividendYield",
    "pe",
    "pb",
    "roe",
    "marketCap",
    "realMoneyIndex",
    "profitGrowth",
]
SortDirection = Literal["asc", "desc"]


class ScreenerFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    preset: str | None
    thresholds: Thresholds
    enabled_thresholds: list[str] | None = Field(default=None, alias="enabledThresholds", max_length=100)
    threshold_directions: dict[str, Literal["gt", "lt", "gte", "lte"]] = Field(default_factory=dict, alias="thresholdDirections")
    exclusions: list[ExclusionKey]
    boards: list[BoardId]
    categories: Categories

    @field_validator("enabled_thresholds", "threshold_directions")
    @classmethod
    def known_thresholds(cls, value):
        if value is not None and any(key not in {row[0] for row in REA_THRESHOLD_ROWS} for key in value):
            raise ValueError("未知筛选字段")
        return value


class SortRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    direction: SortDirection
    enabled: bool
    weight: float = Field(default=100, ge=0, le=100, allow_inf_nan=False)

    @field_validator("field")
    @classmethod
    def known_field(cls, value):
        if value not in {row[0] for row in REA_THRESHOLD_ROWS} | {"profitGrowth", "revenueGrowingFy", "ocfGrowingFy"}:
            raise ValueError("未知排序字段")
        return value


class ScreenerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    filters: ScreenerFilters
    bank: BankRequest | None = None
    sort_rules: list[SortRule] = Field(alias="sortRules")
    page: int = Field(ge=1)
    page_size: int = Field(alias="pageSize", ge=1, le=200)
    sort_mode: Literal["advanced", "quick"] = Field(default="advanced", alias="sortMode")
