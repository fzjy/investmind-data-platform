from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, JSON, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = {"schema": "ops"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    business_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )


class JobCursor(Base):
    __tablename__ = "job_cursor"
    __table_args__ = (
        UniqueConstraint("job_name", "cursor_key", name="uq_job_cursor_job_key"),
        {"schema": "ops"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor_key: Mapped[str] = mapped_column(String(256), nullable=False)
    cursor_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobFailure(Base):
    __tablename__ = "job_failure"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_run_id: Mapped[str | None] = mapped_column(ForeignKey("ops.job_run.id", ondelete="CASCADE"))
    dataset: Mapped[str] = mapped_column(String(128), nullable=False)
    request_key: Mapped[str] = mapped_column(String(256), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(nullable=False, default=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QualityCheckResult(Base):
    __tablename__ = "quality_check_result"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_run_id: Mapped[str | None] = mapped_column(ForeignKey("ops.job_run.id", ondelete="CASCADE"))
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeat"
    __table_args__ = {"schema": "ops"}

    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scheduler_name: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
    )


class SchedulerCheckpoint(Base):
    __tablename__ = "scheduler_checkpoint"
    __table_args__ = {"schema": "ops"}

    checkpoint_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    managed_from: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
    )


class OperationalAlert(Base):
    __tablename__ = "operational_alert"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_key: Mapped[str] = mapped_column(String(256), unique=True)
    alert_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    first_triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
    )


class MacroSeriesState(Base):
    __tablename__ = "macro_series_state"
    __table_args__ = {"schema": "ops"}

    series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_observed_at: Mapped[date | None] = mapped_column(Date)
    latest_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_source: Mapped[str | None] = mapped_column(String(128))
    fallback_used: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    stale: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    stale_reason: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
