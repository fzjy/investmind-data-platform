from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, field_validator


class MacroImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    record_type: Literal["macro"] = Field(alias="recordType")
    source: str
    series_id: str = Field(alias="seriesId")
    name: str
    region: str
    frequency: Literal["daily", "weekly", "monthly", "quarterly", "annual"]
    unit: str
    seasonal_adjustment: Literal["seasonally_adjusted", "not_adjusted", "unknown"] = Field(alias="seasonalAdjustment")
    observed_at: date = Field(alias="observedAt")
    released_at: datetime = Field(alias="releasedAt")
    revision: str
    value: Decimal
    source_url: AnyHttpUrl = Field(alias="sourceUrl")


class ContentImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    record_type: Literal["content"] = Field(alias="recordType")
    source: str
    external_id: str | None = Field(default=None, alias="externalId")
    content_type: Literal["news", "announcement", "article"] = Field(alias="contentType")
    title: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    summary: str | None = None
    body: str | None = None
    published_at: datetime = Field(alias="publishedAt")
    source_url: AnyHttpUrl = Field(alias="sourceUrl")
    securities: list[str] = Field(default_factory=list)
    security_names: list[str] = Field(default_factory=list, alias="securityNames")
    copyright_status: Literal["licensed", "public_domain", "link_only", "unknown", "restricted"] = Field(alias="copyrightStatus")
    ingestion_revision: str | None = Field(default=None, alias="ingestionRevision", max_length=128)

    @field_validator("published_at")
    @classmethod
    def utc_publication_time(cls, value: datetime) -> datetime:
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


ImportRecord = Annotated[MacroImportRecord | ContentImportRecord, Field(discriminator="record_type")]
