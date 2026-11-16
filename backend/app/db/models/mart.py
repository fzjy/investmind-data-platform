from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, case, null, Date, DateTime, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, column_property, attributes
from sqlalchemy.ext.hybrid import hybrid_property

from app.db.base import MartBase


class PageSnapshot(MartBase):
    """市场、宏观、组合等页面的版本化 JSON Mart。"""

    __tablename__ = "page_snapshot"
    __table_args__ = (UniqueConstraint("product", "snapshot_version", name="uq_page_snapshot_product_version"), {"schema": "mart"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_version: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PublishedSnapshot(MartBase):
    __tablename__ = "published_snapshot"
    __table_args__ = {"schema": "mart"}

    product: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    _inline_payload: Mapped[dict[str, Any] | str] = mapped_column("payload", JSON, nullable=False)
    _history_payload = column_property(case((
        func.json_typeof(_inline_payload) == "string",
        select(PageSnapshot.payload).where(PageSnapshot.product == product,
            PageSnapshot.snapshot_version == version).correlate_except(PageSnapshot).scalar_subquery()
    ), else_=null()), expire_on_flush=False)

    @hybrid_property
    def payload(self) -> dict[str, Any]:
        if isinstance(self._inline_payload, str):
            return self._history_payload
        return self._inline_payload

    @payload.inplace.expression
    @classmethod
    def _resolved_payload(cls):
        return func.coalesce(cls._history_payload, cls._inline_payload, type_=JSON)

    @payload.inplace.setter
    def _set_payload(self, value):
        self._inline_payload = value

    def use_history(self, digest: str, value: dict[str, Any]) -> None:
        self._inline_payload = "@investmind-history:" + digest
        attributes.set_committed_value(self, "_history_payload", value)

    stale: Mapped[bool] = mapped_column(nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text)
    freshness_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StockScreenerDaily(MartBase):
    __tablename__ = "stock_screener_daily"
    __table_args__ = (UniqueConstraint("ts_code", "as_of", "snapshot_version", name="uq_screener_security_date_version"), {"schema": "mart"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StockWorkbenchSnapshot(MartBase):
    __tablename__ = "stock_workbench_snapshot"
    __table_args__ = (UniqueConstraint("ts_code", "snapshot_version", name="uq_workbench_security_version"), {"schema": "mart"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StockDailyChart(MartBase):
    __tablename__ = "stock_daily_chart"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", "snapshot_version", name="uq_chart_security_date_version"), {"schema": "mart"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_version: Mapped[str] = mapped_column(String(64), nullable=False)
    point: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
