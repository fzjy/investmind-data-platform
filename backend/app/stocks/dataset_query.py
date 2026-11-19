"""股票日频数据集共用的 Core 读取范围。"""
from collections.abc import Sequence
from datetime import date

from sqlalchemy import and_, func, select

from app.db.models import (
    AdjustmentFactor, ClassificationMember, DailyBasic, DailyPrice, Security, TradeCalendar,
)


def daily_rows_query(
    model: type[DailyPrice] | type[DailyBasic] | type[AdjustmentFactor],
    start: date,
    end: date,
    symbols: Sequence[str] | None,
    *,
    latest_only: bool = False,
):
    conditions = [model.trade_date.between(start, end)]
    if symbols is not None:
        conditions.append(model.ts_code.in_(symbols))
    statement = select(model).where(*conditions)
    if latest_only:
        latest = (
            select(model.ts_code, func.max(model.trade_date).label("trade_date"))
            .where(*conditions)
            .group_by(model.ts_code)
            .subquery()
        )
        statement = statement.join(
            latest,
            and_(model.ts_code == latest.c.ts_code, model.trade_date == latest.c.trade_date),
        )
    return statement


def latest_price_with_basic(symbols, as_of):
    """最近行情决定日期。估值缺失时仍保留行情。"""
    latest = daily_rows_query(DailyPrice, date.min, as_of, symbols, latest_only=True)
    return latest.add_columns(DailyBasic).outerjoin_from(DailyPrice, DailyBasic, and_(
        DailyBasic.ts_code == DailyPrice.ts_code, DailyBasic.trade_date == DailyPrice.trade_date))


def securities_query(symbols=None):
    statement = select(Security)
    if symbols is not None:
        statement = statement.where(Security.ts_code.in_(symbols))
    return statement.order_by(Security.ts_code)


def calendar_query(start, end, exchange=None):
    statement = select(TradeCalendar).where(TradeCalendar.cal_date.between(start, end))
    if exchange is not None:
        statement = statement.where(TradeCalendar.exchange == exchange)
    return statement


def memberships_query(symbols, *, start=None, end=None):
    model = ClassificationMember
    statement = select(model).where(model.ts_code.in_(symbols))
    if start is not None:
        statement = statement.where(model.effective_to.is_(None) | (model.effective_to >= start))
    if end is not None:
        statement = statement.where(model.effective_from <= end)
    return statement


def financial_rows_query(model, symbols, *, start=None, end=None):
    statement = select(model).where(model.ts_code.in_(symbols))
    if start is not None:
        statement = statement.where(model.end_date >= start)
    if end is not None:
        statement = statement.where(model.end_date <= end)
    return statement


def announced_rows_query(model, symbols, *, start=None, end=None):
    statement = select(model).where(model.ts_code.in_(symbols))
    if start is not None:
        statement = statement.where(model.ann_date >= start)
    if end is not None:
        statement = statement.where(model.ann_date <= end)
    return statement
