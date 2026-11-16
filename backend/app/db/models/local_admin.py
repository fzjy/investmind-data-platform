"""本机任务意图与执行记录。"""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Identity, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class LocalAdminTask(Base):
    __tablename__ = 'local_admin_task'
    __table_args__ = {'schema': 'ops'}
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    rule: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    recovered: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    applied: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    effective: Mapped[str] = mapped_column(Text, server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    started: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(128))

class LocalAdminExecution(Base):
    __tablename__ = "local_admin_execution"
    __table_args__ = {"schema": "ops"}
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    recovery: Mapped[bool] = mapped_column(Boolean)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(128))

Index("ix_local_admin_execution_by_task", LocalAdminExecution.name, LocalAdminExecution.id.desc())
