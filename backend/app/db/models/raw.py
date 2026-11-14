from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.base import ExternalBase


class IngestionBatch(Base):
    __tablename__ = "ingestion_batch"
    __table_args__ = {"schema": "raw"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(16))
    business_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourcePayload(Base):
    __tablename__ = "source_payload"
    __table_args__ = (
        UniqueConstraint("batch_id", "request_hash", name="uq_source_payload_batch_request"),
        {"schema": "raw"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("raw.ingestion_batch.id", ondelete="CASCADE"), nullable=False
    )
    api_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RejectedRecord(Base):
    __tablename__ = "rejected_record"
    __table_args__ = {"schema": "raw"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("raw.ingestion_batch.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int | None] = mapped_column(Integer)
    error_path: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_record: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ExternalFile(ExternalBase):
    __tablename__ = "external_file"
    __table_args__ = (UniqueConstraint("file_hash", name="uq_external_file_hash"), {"schema": "raw"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    path_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_summary: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExternalRecord(ExternalBase):
    __tablename__ = "external_record"
    __table_args__ = (UniqueConstraint("external_file_id", "row_number", name="uq_external_record_file_row"), {"schema": "raw"})

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    external_file_id: Mapped[int] = mapped_column(ForeignKey("raw.external_file.id", ondelete="CASCADE"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
