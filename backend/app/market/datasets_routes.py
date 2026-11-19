"""市场公共事实的库存查询；返回各品种自己的单位和日期。"""
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.db.models import DailyBasic
from app.market.commodities.facts import read_asset_daily
from app.market.dataset_query import market_quote_statement
from app.market.etf.facts import stored_trading_components
from app.market.pulse.repository import LINEAGE, validated_statuses
from app.shared.dataset_http import (
    DATASET_ERRORS, INVALID_EVIDENCE,
    DatasetDay, DatasetNumber, DatasetPage, DatasetRange, cursor_date, page_response, reject_duplicate_parameters,
)


AssetCode = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.]{0,31}$")]


class QuoteQuery(DatasetRange):
    asset_code: AssetCode
    asset_type: Literal["indices", "etfs"]


class CommodityQuery(DatasetRange):
    asset_code: AssetCode
    window_end: DatasetDay = Field(description="已采集的完整日历和主力合约映射窗口末日")

    @model_validator(mode="after")
    def within_window(self):
        if self.end_date > self.window_end:
            raise ValueError("end_date 须早于或等于 window_end")
        return self


class EtfQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_code: AssetCode
    trade_date: DatasetDay


class StatusQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
    trade_date: DatasetDay


class SourceRef(BaseModel):
    source_payload_id: int
    captured_at: datetime


class QuoteRow(SourceRef):
    provider: str
    api_name: str
    asset_type: Literal["indices", "etfs"]
    asset_code: str
    trade_date: date
    price_unit: Literal["index_point", "CNY"]
    open: DatasetNumber
    high: DatasetNumber
    low: DatasetNumber
    close: DatasetNumber
    pre_close: DatasetNumber
    pct_chg: DatasetNumber = Field(description="相对前收盘的涨跌幅，%")


class EtfQuote(SourceRef):
    close: DatasetNumber
    pct_chg: DatasetNumber
    vol: DatasetNumber = Field(description="成交量，手")
    amount: DatasetNumber = Field(description="成交额，千元")


class EtfShares(SourceRef):
    fd_share: DatasetNumber = Field(description="基金份额，万份")


class EtfRow(BaseModel):
    asset_code: str
    trade_date: date
    quote: EtfQuote | None
    shares: EtfShares | None


class CommodityRow(BaseModel):
    asset_code: str
    exchange: str
    contract: str
    trade_date: date
    quote_unit: str
    trade_unit: str
    per_unit: DatasetNumber
    pre_close: DatasetNumber
    pre_settle: DatasetNumber
    open: DatasetNumber
    high: DatasetNumber
    low: DatasetNumber
    close: DatasetNumber
    settle: DatasetNumber
    vol: DatasetNumber = Field(description="成交量，手")
    amount: DatasetNumber = Field(description="成交额，万元")
    oi: DatasetNumber = Field(description="持仓量，手")
    sources: dict[str, SourceRef]


class StatusRow(BaseModel):
    symbol: str
    trade_date: date
    limit_status: int
    source: Literal["tushare:daily_basic", "core:daily_basic.data"]
    source_payload_id: int | None


def _etf_component(component):
    if component is None:
        return None
    return {**{k: v for k, v in component.items() if k not in {"sourcePayloadId", "capturedAt"}},
            "source_payload_id": component["sourcePayloadId"], "captured_at": component["capturedAt"]}


def create_router(session_factory, *, catalog=None):
    router = APIRouter(prefix="/api/v1/datasets", tags=["市场数据集"], responses=DATASET_ERRORS,
                       dependencies=[Depends(reject_duplicate_parameters)])

    @router.get("/market-daily", response_model=DatasetPage[QuoteRow], summary="查询单个指数或 ETF 日线")
    def market_daily(query: Annotated[QuoteQuery, Query()]):
        dataset = "market-daily"
        statement = market_quote_statement(query.asset_type, query.start_date, query.end_date,
                                           asset_code=query.asset_code, after=cursor_date(dataset, query))
        with session_factory() as session:
            data = [{**{k: getattr(row, k) for k in QuoteRow.model_fields if k != "price_unit"},
                     "price_unit": "index_point" if row.asset_type == "indices" else "CNY"}
                    for row in session.scalars(statement.limit(query.limit+1))]
        return page_response(dataset, data, query=query, day_field="trade_date")

    @router.get("/etf-trading", response_model=DatasetPage[EtfRow], responses=INVALID_EVIDENCE,
                summary="查询 ETF 当日量额与基金份额")
    def etf_trading(query: Annotated[EtfQuery, Query()]):
        with session_factory() as session:
            quote, shares = stored_trading_components(session, query.asset_code, query.trade_date,
                                                       datetime.now(UTC), catalog=catalog)
        data = [{"asset_code":query.asset_code, "trade_date":query.trade_date,
                 "quote":_etf_component(quote), "shares":_etf_component(shares)}] if quote or shares else []
        return page_response("etf-trading", data)

    @router.get("/commodity-daily", response_model=DatasetPage[CommodityRow], responses=INVALID_EVIDENCE,
                summary="查询商品主力合约及其未复权日线")
    def commodity_daily(query: Annotated[CommodityQuery, Query()]):
        dataset = "commodity-daily"
        after = cursor_date(dataset, query)
        with session_factory() as session:
            facts = read_asset_daily(session, catalog, query.asset_code, query.window_end, datetime.now(UTC),
                                    requested_start=query.start_date)
        data = [row for row in facts if query.start_date.isoformat() <= row["trade_date"] <= query.end_date.isoformat()
                and (after is None or row["trade_date"] > after.isoformat())]
        return page_response(dataset, data, query=query, day_field="trade_date")

    @router.get("/stock-limit-status", response_model=DatasetPage[StatusRow], summary="查询股票当日涨跌停状态码")
    def stock_limit_status(query: Annotated[StatusQuery, Query()]):
        with session_factory() as session:
            basic = session.scalar(select(DailyBasic).filter_by(ts_code=query.symbol, trade_date=query.trade_date))
            statuses = validated_statuses(session, [basic] if basic is not None else [], datetime.now(UTC))
            rows = []
            if query.symbol in statuses:
                proof = basic.data.get(LINEAGE)
                rows.append({"symbol": query.symbol, "trade_date": query.trade_date,
                             "limit_status": statuses[query.symbol],
                             "source": "tushare:daily_basic" if proof else "core:daily_basic.data",
                             "source_payload_id": proof["sourcePayloadId"] if proof else None})
        return page_response("stock-limit-status", rows)

    return router
