"""订单字段独立证据；永不写入应收空间或原财报JSON。"""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class OrderAttempt(Base):
    __tablename__ = 'enterprise_order_attempt'
    __table_args__ = (CheckConstraint("state IN ('reserved','received','failed')", name='order_attempt_state'), {'schema':'ops'})
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    request_key: Mapped[str] = mapped_column(String(64), index=True)
    plan_fingerprint: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    page_size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderResponse(Base):
    __tablename__ = 'enterprise_order_response'
    __table_args__ = {'schema':'raw'}
    attempt_id: Mapped[str] = mapped_column(ForeignKey('ops.enterprise_order_attempt.id'), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    response_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderSupplement(Base):
    __tablename__ = 'enterprise_order_supplement'
    __table_args__ = (CheckConstraint("status IN ('matched','returned-null','source-witness-missing',"
        "'response-witness-missing','no-match','ambiguous','missing-field','invalid-value','truncated','identity-mismatch')",
        name='order_supplement_status'),
        CheckConstraint("(status = 'matched' AND value IS NOT NULL) OR (status <> 'matched' AND value IS NULL)",
            name='order_supplement_value'), {'schema':'core'})
    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_id: Mapped[str] = mapped_column(ForeignKey('raw.enterprise_order_response.attempt_id'))
    core_binding: Mapped[dict] = mapped_column(JSON)
    witness_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    value: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
