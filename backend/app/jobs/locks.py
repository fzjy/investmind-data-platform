from __future__ import annotations

import hashlib
from datetime import date
from threading import Lock
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


class JobLock(Protocol):
    def try_acquire(self, value: str) -> bool: ...

    def release(self, value: str) -> None: ...


def lock_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def daily_sync_lock_key(business_date: date) -> str:
    return f"sync-daily:{business_date.isoformat()}"


def macro_sync_lock_key(business_key: str) -> str:
    return f"sync-macro:{business_key}"


def data_publication_lock_key() -> str:
    return "data-publication"


class ProcessLocalJobLock:
    """Process-local fallback using the PostgreSQL adapter's key semantics."""

    _guard = Lock()
    _held: set[int] = set()

    def try_acquire(self, value: str) -> bool:
        key = lock_key(value)
        with self._guard:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, value: str) -> None:
        with self._guard:
            self._held.discard(lock_key(value))


class PostgresAdvisoryJobLock:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connections: dict[int, Connection] = {}
        self._guard = Lock()

    def try_acquire(self, value: str) -> bool:
        key = lock_key(value)
        with self._guard:
            if key in self._connections:
                return False
            connection = self._engine.connect()
            try:
                acquired = bool(
                    connection.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": key},
                    )
                )
                if not acquired:
                    connection.close()
                    return False
                self._connections[key] = connection
                return True
            except Exception:
                connection.close()
                raise

    def release(self, value: str) -> None:
        key = lock_key(value)
        with self._guard:
            connection = self._connections.pop(key, None)
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": key},
            )
        finally:
            connection.close()


def job_lock_for_engine(engine: Engine) -> JobLock:
    return PostgresAdvisoryJobLock(engine)
