from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.base import ExternalBase


class Security(Base):
    __tablename__ = "security"
    __table_args__ = {"schema": "core"}

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(8))
    board: Mapped[str | None] = mapped_column(String(32))
    list_date: Mapped[date | None] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(32))
    is_st: Mapped[bool] = mapped_column(nullable=False, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="tushare")
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecurityNameHistory(Base):
    __tablename__ = "security_name_history"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ListingStatusHistory(Base):
    __tablename__ = "listing_status_history"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)


class TradeCalendar(Base):
    __tablename__ = "trade_calendar"
    __table_args__ = (UniqueConstraint("exchange", "cal_date", name="uq_trade_calendar_exchange_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    cal_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_open: Mapped[bool] = mapped_column(nullable=False)
    pretrade_date: Mapped[date | None] = mapped_column(Date)


def _classification_source(context):
    value = (context.get_current_parameters().get("data") or {}).get("src")
    return value.strip() if isinstance(value, str) and value.strip() else "legacy_unknown"


class Classification(Base):
    __tablename__ = "classification"
    __table_args__ = (UniqueConstraint("provider", "source", "classification_id", name="uq_classification_provider_source_id"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default=_classification_source)
    classification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[int | None] = mapped_column()
    parent_id: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ClassificationMember(Base):
    __tablename__ = "classification_member"
    __table_args__ = (UniqueConstraint("classification_id", "ts_code", "effective_from", name="uq_classification_member_period"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    classification_id: Mapped[int] = mapped_column(ForeignKey("core.classification.id"), nullable=False)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IndexConstituent(Base):
    __tablename__ = "index_constituent"
    __table_args__ = (UniqueConstraint("index_code", "ts_code", "effective_from", name="uq_index_constituent_period"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    index_code: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))


class DailyPrice(Base):
    __tablename__ = "daily_price"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_daily_price_security_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    close: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pre_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    change: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pct_chg: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    vol: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyBasic(Base):
    __tablename__ = "daily_basic"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_daily_basic_security_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pe: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    ps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    total_mv: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    circ_mv: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    dv_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    dv_ttm: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AdjustmentFactor(Base):
    __tablename__ = "adjustment_factor"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_adjustment_factor_security_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    adj_factor: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))


class PriceLimit(Base):
    __tablename__ = "price_limit"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_price_limit_security_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    up_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    down_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))


class Suspension(Base):
    __tablename__ = "suspension"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_suspension_security_date"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    suspend_type: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)


class FinancialBase:
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    f_ann_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_type: Mapped[str | None] = mapped_column(String(32))
    comp_type: Mapped[str | None] = mapped_column(String(32))
    update_flag: Mapped[str | None] = mapped_column(String(8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IncomeStatement(FinancialBase, Base):
    __tablename__ = "income_statement"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_income_statement_version"), {"schema": "core"})


class BalanceSheet(FinancialBase, Base):
    __tablename__ = "balance_sheet"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_balance_sheet_version"), {"schema": "core"})


class CashflowStatement(FinancialBase, Base):
    __tablename__ = "cashflow_statement"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_cashflow_statement_version"), {"schema": "core"})


class FinancialIndicator(FinancialBase, Base):
    __tablename__ = "financial_indicator"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_financial_indicator_version"), {"schema": "core"})


class FinancialForecast(FinancialBase, Base):
    __tablename__ = "financial_forecast"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_financial_forecast_version"), {"schema": "core"})


class FinancialExpress(FinancialBase, Base):
    __tablename__ = "financial_express"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "report_type", "ann_date", "update_flag", name="uq_financial_express_version"), {"schema": "core"})


class Dividend(Base):
    __tablename__ = "dividend"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", "ann_date", "div_proc", name="uq_dividend_version"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    div_proc: Mapped[str | None] = mapped_column(String(32))
    cash_div: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cash_div_tax: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    record_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    pay_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Repurchase(Base):
    __tablename__ = "repurchase"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    proc: Mapped[str | None] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ShareUnlock(Base):
    __tablename__ = "share_unlock"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    float_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DisclosureSchedule(Base):
    __tablename__ = "disclosure_schedule"
    __table_args__ = (
        UniqueConstraint(
            "ts_code",
            "end_date",
            name="uq_disclosure_schedule_period",
        ),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(
        ForeignKey("core.security.ts_code"),
        nullable=False,
    )
    ann_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    pre_date: Mapped[date | None] = mapped_column(Date)
    actual_date: Mapped[date | None] = mapped_column(Date)
    modify_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ShareholderCount(Base):
    __tablename__ = "shareholder_count"
    __table_args__ = (UniqueConstraint("ts_code", "end_date", name="uq_shareholder_count_period"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    holder_num: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class TopHolder(Base):
    __tablename__ = "top_holder"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    ann_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    holder_type: Mapped[str | None] = mapped_column(String(32))
    holder_name: Mapped[str | None] = mapped_column(String(256))
    hold_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class BusinessSegment(Base):
    __tablename__ = "business_segment"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(ForeignKey("core.security.ts_code"), nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    segment_type: Mapped[str | None] = mapped_column(String(32))
    segment_name: Mapped[str | None] = mapped_column(String(256))
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class MacroObservation(ExternalBase):
    __tablename__ = "macro_observation"
    __table_args__ = (UniqueConstraint("source", "series_id", "observed_at", "released_at", "revision", name="uq_macro_observation_revision"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    seasonal_adjustment: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[date] = mapped_column(Date, nullable=False)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContentDocument(ExternalBase):
    __tablename__ = "content_document"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_content_document_dedupe"), {"schema": "core"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256))
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    securities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    copyright_status: Mapped[str] = mapped_column(String(32), nullable=False)
