"""提供获准展示的库存新闻、公告及文章。"""
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import tuple_

from app.content.content_policy import permitted_text
from app.content.dataset_query import visible_documents
from app.db.models import ContentDocument
from app.shared.errors import DataServiceError
from app.shared.dataset_http import DATASET_ERRORS, reject_duplicate_parameters


def _utc_time(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("发布时间须带时区")
    try:
        return stamp.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("发布时间超出 UTC 时间范围") from None


class DocumentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal["news", "announcement", "article"] | None = None
    symbol: str | None = Field(None, pattern=r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
    published_from: AwareDatetime | None = Field(None, description="含起点，带时区的发布时间")
    published_through: AwareDatetime | None = Field(None, description="含终点，未来内容仍以当前时间截断")
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = Field(None, min_length=1, max_length=512)

    @field_validator("published_from", "published_through", mode="before")
    @classmethod
    def utc_time(cls, value):
        return _utc_time(value) if value is not None else None

    @model_validator(mode="after")
    def ordered(self):
        if self.published_from and self.published_through and self.published_from > self.published_through:
            raise ValueError("发布时间区间起点晚于终点")
        return self


class DocumentRow(BaseModel):
    id: int
    source: str
    external_id: str | None
    content_type: str
    published_at: AwareDatetime
    title: str
    source_url: str
    securities: list[str]
    copyright_status: str
    body: str | None
    summary: str | None


class DocumentPageMeta(BaseModel):
    dataset: Literal["content-documents"] = "content-documents"
    coverage_scope: Literal["stored"] = "stored"
    next_cursor: str | None


class DocumentPage(BaseModel):
    data: list[DocumentRow]
    meta: DocumentPageMeta


def _position(token, scope):
    try:
        data = base64.b64decode(token, altchars=b"-_", validate=True)
        bound, stamp, identity = json.loads(data)
        stamp = _utc_time(stamp)
        if bound != scope or type(identity) is not int or not 0 < identity <= 2147483647:
            raise ValueError
        return stamp, identity
    except (TypeError, ValueError, OverflowError):
        raise DataServiceError("INVALID_REQUEST", "内容游标无效或筛选条件已改变") from None


def create_router(factory):
    router = APIRouter(tags=["财经内容数据集"], responses=DATASET_ERRORS,
                       dependencies=[Depends(reject_duplicate_parameters)])

    @router.get("/api/v1/datasets/content-documents", response_model=DocumentPage,
                summary="按发布时间查询可展示的内容")
    def documents(query: Annotated[DocumentQuery, Query()]):
        scope = sha256(query.model_dump_json(exclude={"cursor", "limit"}).encode()).hexdigest()
        now = datetime.now(timezone.utc)
        statement = visible_documents(min(query.published_through or now, now), content_type=query.content_type,
                                      symbol=query.symbol, published_from=query.published_from)
        if query.cursor:
            statement = statement.where(tuple_(ContentDocument.published_at, ContentDocument.id)
                                        < _position(query.cursor, scope))
        with factory() as session:
            rows = session.scalars(statement.limit(query.limit + 1)).all()
            data = [DocumentRow(id=row.id, source=row.source, external_id=row.external_id,
                                content_type=row.content_type, published_at=row.published_at, title=row.title,
                                source_url=row.source_url, securities=row.securities,
                                copyright_status=row.copyright_status,
                                **permitted_text(row.copyright_status, row.summary, row.body))
                    for row in rows[:query.limit]]
        cursor = None
        if len(rows) > query.limit:
            key = [scope, data[-1].published_at.isoformat(), data[-1].id]
            cursor = base64.urlsafe_b64encode(json.dumps(key).encode()).decode()
        return DocumentPage(data=data, meta=DocumentPageMeta(next_cursor=cursor))

    return router
