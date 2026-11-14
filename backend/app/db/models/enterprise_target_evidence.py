"""成分候选独立metadata，避免0001动态建表提前接管0016。"""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TargetEvidenceBase


class TargetObservation(TargetEvidenceBase):
    __tablename__ = 'enterprise_target_observation'
    __table_args__ = (CheckConstraint("provider = 'tushare'", name='target_provider'), {'schema': 'raw'})
    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    target_code: Mapped[str] = mapped_column(String(64))
    target_kind: Mapped[str] = mapped_column(String(16))
    api_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON)
    requested_fields: Mapped[list] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    response_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TargetCandidate(TargetEvidenceBase):
    __tablename__ = 'enterprise_target_candidate'
    __table_args__ = (UniqueConstraint('observation_id', 'normalizer_version', 'policy_hash', 'as_of', 'timezone',
        name='uq_enterprise_target_candidate_context'), {'schema': 'core'})
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey('raw.enterprise_target_observation.observation_id'))
    normalizer_version: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
