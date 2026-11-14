"""REA多字段独立证据，绝不覆盖旧财报/应收/订单。"""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ReaInputPlan(Base):
    __tablename__ = 'enterprise_rea_plan'
    __table_args__ = {'schema': 'ops'}
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence_number: Mapped[int] = mapped_column(Integer, unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    witness_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReaInputAttempt(Base):
    __tablename__ = 'enterprise_rea_attempt'
    __table_args__ = (CheckConstraint("state IN ('reserved','received','failed')", name='rea_attempt_state'), {'schema': 'ops'})
    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_fingerprint: Mapped[str] = mapped_column(ForeignKey('ops.enterprise_rea_plan.fingerprint'))
    run_id: Mapped[str] = mapped_column(String(36))
    request: Mapped[dict] = mapped_column(JSON)
    page_size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReaInputResponse(Base):
    __tablename__ = 'enterprise_rea_response'
    __table_args__ = {'schema': 'raw'}
    request_key: Mapped[str] = mapped_column(ForeignKey('ops.enterprise_rea_attempt.request_key'), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    response_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReaInputSupplement(Base):
    __tablename__ = 'enterprise_rea_supplement'
    __table_args__ = (CheckConstraint("status IN ('matched','response-witness-missing','no-match','ambiguous','truncated','identity-mismatch')", name='rea_supplement_status'), {'schema': 'core'})
    request_key: Mapped[str] = mapped_column(ForeignKey('raw.enterprise_rea_response.request_key'), primary_key=True)
    core_binding: Mapped[dict] = mapped_column(JSON)
    witness_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
