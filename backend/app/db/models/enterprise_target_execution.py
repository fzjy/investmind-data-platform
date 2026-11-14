"""单bootstrap根及发送前预留；不注册到早期Base。"""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import TargetEvidenceBase


class TargetExecutionPlan(TargetEvidenceBase):
    __tablename__='enterprise_target_execution_plan'
    __table_args__=(CheckConstraint("slot = 'bootstrap'",name='target_execution_slot'),{'schema':'ops'})
    slot: Mapped[str]=mapped_column(String(32),primary_key=True)
    fingerprint: Mapped[str]=mapped_column(String(64),unique=True)
    snapshot: Mapped[dict]=mapped_column(JSON)
    snapshot_hash: Mapped[str]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())


class TargetExecutionAttempt(TargetEvidenceBase):
    __tablename__='enterprise_target_execution_attempt'
    __table_args__=(CheckConstraint("state = 'reserved' OR state = 'received' OR state = 'failed'",name='target_execution_state'),{'schema':'ops'})
    request_key: Mapped[str]=mapped_column(String(64),primary_key=True)
    plan_fingerprint: Mapped[str]=mapped_column(ForeignKey('ops.enterprise_target_execution_plan.fingerprint'))
    run_id: Mapped[str]=mapped_column(String(36))
    request: Mapped[dict]=mapped_column(JSON)
    request_hash: Mapped[str]=mapped_column(String(64))
    state: Mapped[str]=mapped_column(String(16))
    observation_id: Mapped[str | None]=mapped_column(ForeignKey('raw.enterprise_target_observation.observation_id'))
    error_code: Mapped[str | None]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
