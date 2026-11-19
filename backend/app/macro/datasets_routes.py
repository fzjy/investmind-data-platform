"""查询标准宏观系列在指定发布时点可见的观测。"""
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.dataset_query import observation_page
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.facts import read_extension_sources
from app.macro.extensions.normalize import quarter_date
from app.macro.extensions.plan import CONTRACTS
from app.shared.dataset_http import (
    DATASET_ERRORS, INVALID_EVIDENCE,
    DatasetNumber, DatasetPage, DatasetRange, cursor_date, page_response, reject_duplicate_parameters,
)
from app.shared.errors import DataServiceError


class ObservationQuery(DatasetRange):
    series_id: str = Field(min_length=1, max_length=128, description="标准宏观指标 ID，如 cn.cpi.yoy")
    as_of: AwareDatetime = Field(description="按库存记载的发布时间选版，须带时区")

    @field_validator("as_of", mode="before")
    @classmethod
    def iso_cutoff(cls, value):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @field_validator("as_of")
    @classmethod
    def utc_cutoff(cls, value):
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("as_of 超出 UTC 时间范围") from None


class ObservationRow(BaseModel):
    series_id: str
    name: str
    source: str
    source_url: str
    region: str
    frequency: str
    unit: str
    seasonal_adjustment: str
    observed_at: date
    released_at: datetime
    revision: str
    value: DatasetNumber


class SpecialtyQuery(ObservationQuery):
    series_id: Literal["cn.gdp.nominal_cumulative", "fx.usdcnh"]
    as_of: AwareDatetime = Field(description="只包含截至此刻已完成作业并绑定 Core 的采集窗口，须带时区")


class SpecialtyRow(BaseModel):
    series_id: str
    period: str
    observed_at: date
    frequency: str
    value: DatasetNumber
    unit: str
    source: str
    source_url: str
    quote_side: str | None
    retrieved_at: datetime
    evidence_fingerprint: str
    source_row: int


def create_router(session_factory, *, catalog=None):
    router = APIRouter(prefix="/api/v1/datasets", tags=["宏观数据集"], responses=DATASET_ERRORS,
                       dependencies=[Depends(reject_duplicate_parameters)])

    @router.get("/macro-observations", response_model=DatasetPage[ObservationRow],
                summary="按指标、观测期和发布截止时刻查询宏观观测")
    def macro_observations(query: Annotated[ObservationQuery, Query()]):
        name = "macro-observations"
        selected_catalog = catalog or MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        if query.series_id not in {item.id for item in selected_catalog.series}:
            raise DataServiceError("INVALID_REQUEST", "未知的标准宏观指标")
        after = cursor_date(name, query)
        with session_factory() as session:
            selected = observation_page(session, selected_catalog, query.series_id, query.as_of, query.start_date,
                                        query.end_date, after, query.limit+1)
            data = [{key: getattr(row, key) for key in ObservationRow.model_fields} for row in selected]
        return page_response(name, data, query=query, day_field="observed_at", coverage_scope="release_as_of")

    @router.get("/macro-specialty-observations", response_model=DatasetPage[SpecialtyRow], responses=INVALID_EVIDENCE,
                summary="查询截至采集完成时点可见的累计名义 GDP 或离岸人民币汇率")
    def macro_specialty_observations(query: Annotated[SpecialtyQuery, Query()]):
        dataset = "macro-specialty-observations"
        group = "nominalGdp" if query.series_id == "cn.gdp.nominal_cumulative" else "usdcnh"
        after = cursor_date(dataset, query)
        with session_factory() as session:
            try:
                evidence = read_extension_sources(session, query.as_of, group_id=group)[group]
            except ExtensionError:
                raise DataServiceError("INVALID_SOURCE_PAYLOAD", "专项观测来源凭证校验失败") from None
        rows = []
        for point in sorted(evidence["points"], key=lambda p: p["period"]):
            day = quarter_date(point["period"]) if group == "nominalGdp" else date.fromisoformat(point["period"])
            if query.start_date <= day <= query.end_date and (after is None or day > after):
                rows.append({"series_id":query.series_id, "period":point["period"], "observed_at":day,
                    "frequency":evidence["frequency"], "value":Decimal(point["value"]), "unit":evidence["unit"],
                    "source":CONTRACTS[group]["source"], "source_url":CONTRACTS[group]["sourceUrl"],
                    "quote_side":evidence["quoteSide"], "retrieved_at":evidence["retrievedAt"],
                    "evidence_fingerprint":evidence["evidenceFingerprint"], "source_row":point["sourceRow"]})
        return page_response(dataset, rows, query=query, day_field="observed_at", coverage_scope="capture_as_of")

    return router
