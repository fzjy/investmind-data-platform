"""提供当前 Core 库存的股票日线、复权因子和每日估值查询。"""
from __future__ import annotations

import base64
import json
from datetime import date
from typing import Annotated, Generic, Literal, TypeVar

import simplejson
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import tuple_

from app.db.models import AdjustmentFactor, DailyBasic, DailyPrice
from app.shared.dataset_http import DATASET_ERRORS, DatasetNumber, reject_duplicate_parameters
from app.shared.errors import DataServiceError
from app.shared.stock_mapping import finite_decimal
from app.stocks.dataset_query import daily_rows_query
from app.stocks.universes.resolver import STOCK_SYMBOL_PATTERN


class StockDatasetQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str | None = Field(None, pattern=STOCK_SYMBOL_PATTERN.pattern, description="完整证券代码，如 600519.SH")
    trade_date: date | None = Field(None, description="按单个交易日查询，可加证券代码")
    start_date: date | None = Field(None, description="单股区间的起始日期，包含当天")
    end_date: date | None = Field(None, description="单股区间的结束日期，包含当天")
    limit: int = Field(500, ge=1, le=2000, description="每页最多返回的行数")
    cursor: str | None = Field(None, min_length=1, max_length=256, description="同一查询上一页返回的 next_cursor")

    @field_validator("trade_date", "start_date", "end_date", mode="before")
    @classmethod
    def iso_day(cls, value):
        if isinstance(value, str) and date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期须使用 YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.trade_date is not None:
            if self.start_date is not None or self.end_date is not None:
                raise ValueError("按交易日查询时仅传 trade_date")
        elif self.symbol is None or self.start_date is None or self.end_date is None:
            raise ValueError("区间查询需要 symbol、start_date 和 end_date")
        elif self.start_date > self.end_date:
            raise ValueError("start_date 必须早于或等于 end_date")
        return self


class StockDay(BaseModel):
    symbol: str
    trade_date: date


class DailyQuoteRow(StockDay):
    open: DatasetNumber | None
    high: DatasetNumber | None
    low: DatasetNumber | None
    close: DatasetNumber | None
    pre_close: DatasetNumber | None
    change: DatasetNumber | None
    pct_chg: DatasetNumber | None
    vol: DatasetNumber | None
    amount: DatasetNumber | None


class AdjustmentRow(StockDay):
    adj_factor: DatasetNumber | None


class DailyBasicRow(StockDay):
    turnover_rate: DatasetNumber | None
    pe: DatasetNumber | None
    pe_ttm: DatasetNumber | None
    pb: DatasetNumber | None
    ps: DatasetNumber | None
    ps_ttm: DatasetNumber | None
    total_mv: DatasetNumber | None
    circ_mv: DatasetNumber | None
    dv_ratio: DatasetNumber | None
    dv_ttm: DatasetNumber | None
    total_share: DatasetNumber | None
    float_share: DatasetNumber | None
    free_share: DatasetNumber | None


# 这五项只存于 daily_basic.data。其余数值均读类型化列。
BASIC_JSON_FIELDS = ("pe_ttm", "ps_ttm", "total_share", "float_share", "free_share")


class DatasetMeta(BaseModel):
    dataset: str
    coverage_scope: Literal["stored"] = Field("stored", description="当前库中已保存的数据范围")
    next_cursor: str | None


Row = TypeVar("Row", bound=StockDay)


class DatasetPage(BaseModel, Generic[Row]):
    data: list[Row]
    meta: DatasetMeta


def _cursor_position(token, scope):
    try:
        payload = base64.b64decode(token, altchars=b"-_", validate=True)
        saved_scope, day, code = json.loads(payload)
        if saved_scope != scope or not isinstance(code, str) or not STOCK_SYMBOL_PATTERN.fullmatch(code):
            raise ValueError
        return date.fromisoformat(day), code
    except (ValueError, TypeError, UnicodeError):
        raise DataServiceError("INVALID_REQUEST", "分页游标无效或查询条件已改变") from None


def _read_page(factory, dataset, model, row_type, query):
    start = query.trade_date or query.start_date
    end = query.trade_date or query.end_date
    symbols = (query.symbol,) if query.symbol is not None else None
    scope = [dataset, query.symbol, start.isoformat(), end.isoformat()]
    columns = [model.ts_code.label("symbol"), model.trade_date]
    value_fields = [name for name in row_type.model_fields if name not in StockDay.model_fields]
    for name in value_fields:
        if name in BASIC_JSON_FIELDS:
            columns.append(model.data[name].as_string().label(name))
        else:
            columns.append(getattr(model, name))
    statement = daily_rows_query(model, start, end, symbols).with_only_columns(*columns)
    if query.cursor is not None:
        statement = statement.where(
            tuple_(model.trade_date, model.ts_code) > _cursor_position(query.cursor, scope)
        )
    statement = statement.order_by(model.trade_date, model.ts_code).limit(query.limit + 1)
    with factory() as session:
        rows = session.execute(statement).mappings().all()

    data = [
        {
            "symbol": row["symbol"],
            "trade_date": row["trade_date"].isoformat(),
            **{name: finite_decimal(row[name]) for name in value_fields},
        }
        for row in rows[:query.limit]
    ]
    next_cursor = None
    if len(rows) > query.limit:
        last = data[-1]
        payload = json.dumps([scope, last["trade_date"], last["symbol"]])
        next_cursor = base64.urlsafe_b64encode(payload.encode()).decode()
    page = {"data": data, "meta": {"dataset": dataset, "coverage_scope": "stored", "next_cursor": next_cursor}}
    return Response(simplejson.dumps(page, use_decimal=True, separators=(",", ":")), media_type="application/json")


def create_router(session_factory):
    from app.stocks.reference_routes import create_reference_router
    router = APIRouter(
        prefix="/api/v1/datasets",
        tags=["股票数据集"],
        dependencies=[Depends(reject_duplicate_parameters)],
        responses=DATASET_ERRORS,
    )

    @router.get("/stock-daily", response_model=DatasetPage[DailyQuoteRow], summary="查询股票未复权日线")
    def stock_daily(query: Annotated[StockDatasetQuery, Query()]):
        return _read_page(session_factory, "stock-daily", DailyPrice, DailyQuoteRow, query)

    @router.get("/stock-adjustment", response_model=DatasetPage[AdjustmentRow], summary="查询股票复权因子")
    def stock_adjustment(query: Annotated[StockDatasetQuery, Query()]):
        return _read_page(session_factory, "stock-adjustment", AdjustmentFactor, AdjustmentRow, query)

    @router.get("/stock-daily-basic", response_model=DatasetPage[DailyBasicRow], summary="查询每日估值与股本")
    def stock_daily_basic(query: Annotated[StockDatasetQuery, Query()]):
        return _read_page(session_factory, "stock-daily-basic", DailyBasic, DailyBasicRow, query)

    router.include_router(create_reference_router(session_factory))
    return router
