"""单序列、日期唯一的库存查询使用的 HTTP 合同。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

import simplejson
from fastapi import Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema, model_validator

from app.shared.errors import DataServiceError


DatasetNumber = Annotated[Decimal, WithJsonSchema({"type": "number"}, mode="serialization")]
Row = TypeVar("Row")
DATASET_ERRORS = {
    400: {"description": "查询参数或分页游标无效"},
    503: {"description": "数据库暂不可用"},
    "default": {"description": "服务端错误"},
}
INVALID_EVIDENCE = {502: {"description": "数据来源凭证有冲突或未通过校验"}}


def _calendar_date(value):
    if isinstance(value, str) and date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期须使用 YYYY-MM-DD")
    return value


DatasetDay = Annotated[date, BeforeValidator(_calendar_date)]


class DatasetRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: DatasetDay = Field(description="业务日期起点，包含当天")
    end_date: DatasetDay = Field(description="业务日期终点，包含当天")
    limit: int = Field(500, ge=1, le=2000)
    cursor: str | None = Field(None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date > self.end_date:
            raise ValueError("start_date 必须早于或等于 end_date")
        return self


class DatasetMeta(BaseModel):
    dataset: str
    coverage_scope: Literal["stored", "release_as_of", "capture_as_of"]
    next_cursor: str | None


class DatasetPage(BaseModel, Generic[Row]):
    data: list[Row]
    meta: DatasetMeta


def reject_duplicate_parameters(request: Request) -> None:
    for key in request.query_params:
        if len(request.query_params.getlist(key)) > 1:
            raise DataServiceError("INVALID_REQUEST", f"查询参数 {key} 只能传一次")


def _scope(dataset: str, query: DatasetRange) -> str:
    filters = query.model_dump(mode="json", exclude={"limit", "cursor"})
    return hashlib.sha256(json.dumps([dataset, filters], sort_keys=True).encode()).hexdigest()


def cursor_date(dataset: str, query: DatasetRange) -> date | None:
    if query.cursor is None:
        return None
    try:
        raw = base64.b64decode(query.cursor, altchars=b"-_", validate=True)
        scope, last = json.loads(raw)
        day = date.fromisoformat(last)
        if scope != _scope(dataset, query) or not query.start_date <= day <= query.end_date:
            raise ValueError
        return day
    except (ValueError, TypeError, UnicodeError):
        raise DataServiceError("INVALID_REQUEST", "分页游标无效或查询条件已改变") from None


def _iso_date(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"不能序列化 {type(value).__name__}")


def page_response(dataset, rows, *, query=None, day_field=None, coverage_scope="stored"):
    next_cursor = None
    if query is not None and len(rows) > query.limit:
        rows = rows[:query.limit]
        position = [_scope(dataset, query), rows[-1][day_field]]
        next_cursor = base64.urlsafe_b64encode(json.dumps(position, default=_iso_date).encode()).decode()
    page = {"data": rows, "meta": {
        "dataset": dataset, "coverage_scope": coverage_scope, "next_cursor": next_cursor}}
    return Response(simplejson.dumps(page, use_decimal=True, default=_iso_date, separators=(",", ":")),
                    media_type="application/json")
