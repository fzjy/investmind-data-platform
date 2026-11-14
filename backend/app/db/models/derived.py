from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, JSON, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import DerivedBase


class MetricValue(DerivedBase):
    __tablename__ = "metric_value"
    __table_args__ = (UniqueConstraint("ts_code", "metric_id", "as_of", "formula_version", name="uq_metric_value_version"), {"schema": "derived"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(128), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    unit: Mapped[str | None] = mapped_column(String(32))
    formula_version: Mapped[str] = mapped_column(String(32), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    calculation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MetricInput(DerivedBase):
    __tablename__ = "metric_input"
    __table_args__ = {"schema": "derived"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    metric_value_id: Mapped[int] = mapped_column(ForeignKey("derived.metric_value.id", ondelete="CASCADE"), nullable=False)
    input_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_table: Mapped[str] = mapped_column(String(128), nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))


class RiskResult(DerivedBase):
    __tablename__ = "risk_result"
    __table_args__ = (
        UniqueConstraint(
            "ts_code",
            "risk_id",
            "as_of",
            name="uq_risk_result_security_rule_date",
        ),
        Index("uq_current_risk_security_rule", "ts_code", "risk_id", unique=True,
              postgresql_where=text("snapshot_version IS NOT NULL"), sqlite_where=text("snapshot_version IS NOT NULL")),
        {"schema": "derived"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_version: Mapped[str | None] = mapped_column(String(64))
    rule_version: Mapped[str | None] = mapped_column(String(64))
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)


class MacroXrayResult(DerivedBase):
    __tablename__ = "macro_xray_result"
    __table_args__ = {"schema": "derived"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(32), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
