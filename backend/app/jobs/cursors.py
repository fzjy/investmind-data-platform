from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobCursor


class CursorStore(Protocol):
    def load(self, cursor_key: str) -> dict[str, Any] | None: ...

    def save(self, cursor_key: str, cursor_value: Mapping[str, Any]) -> None: ...

    def delete(self, cursor_key: str) -> None: ...


class SqlJobCursorRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        job_name: str = "backfill",
    ) -> None:
        self._session_factory = session_factory
        self._job_name = job_name

    def load(self, cursor_key: str) -> dict[str, Any] | None:
        with self._session_factory.begin() as session:
            cursor = session.scalar(
                select(JobCursor).where(
                    JobCursor.job_name == self._job_name,
                    JobCursor.cursor_key == cursor_key,
                )
            )
            if cursor is None:
                return None
            return dict(cursor.cursor_value)

    def save(self, cursor_key: str, cursor_value: Mapping[str, Any]) -> None:
        with self._session_factory.begin() as session:
            cursor = session.scalar(
                select(JobCursor).where(
                    JobCursor.job_name == self._job_name,
                    JobCursor.cursor_key == cursor_key,
                )
            )
            if cursor is None:
                session.add(
                    JobCursor(
                        job_name=self._job_name,
                        cursor_key=cursor_key,
                        cursor_value=dict(cursor_value),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                return
            cursor.cursor_value = dict(cursor_value)
            cursor.updated_at = datetime.now(timezone.utc)

    def delete(self, cursor_key: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                delete(JobCursor).where(
                    JobCursor.job_name == self._job_name,
                    JobCursor.cursor_key == cursor_key,
                )
            )
