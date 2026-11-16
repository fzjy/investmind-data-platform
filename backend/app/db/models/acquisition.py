from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DataAcquisitionState(Base):
    __tablename__ = 'data_acquisition_state'
    __table_args__ = (
        CheckConstraint("scope_kind in ('routine','historical')", name='scope_kind'),
        Index('ix_acquisition_subject', 'subject_kind', 'subject_key', 'entry_id'),
        {'schema':'ops'},
    )
    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    subject_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool]
    scope: Mapped[dict] = mapped_column(JSON)
    trigger: Mapped[str] = mapped_column(String(32))
    terminal: Mapped[dict] = mapped_column(JSON)
    last_success: Mapped[dict] = mapped_column(JSON)
    failures: Mapped[dict] = mapped_column(JSON)
