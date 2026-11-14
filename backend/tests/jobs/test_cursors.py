from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import JobCursor


def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
                "derived": None,
                "mart": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def test_load_returns_none_when_cursor_is_missing() -> None:
    from app.jobs.cursors import SqlJobCursorRepository

    engine, factory = session_factory()

    repository = SqlJobCursorRepository(factory)

    assert repository.load("daily:600519.SH") is None
    engine.dispose()


def test_save_then_load_returns_independent_dictionary() -> None:
    from app.jobs.cursors import SqlJobCursorRepository

    engine, factory = session_factory()
    repository = SqlJobCursorRepository(factory)

    payload = {"page": 1, "trade_date": "20260817"}
    repository.save("daily:600519.SH", payload)
    payload["page"] = 99

    loaded = repository.load("daily:600519.SH")

    assert loaded == {"page": 1, "trade_date": "20260817"}
    assert loaded is not payload
    loaded["page"] = 2
    assert repository.load("daily:600519.SH") == {"page": 1, "trade_date": "20260817"}
    engine.dispose()


def test_second_save_upserts_same_row_and_advances_updated_at(monkeypatch) -> None:
    import app.jobs.cursors as cursors

    engine, factory = session_factory()
    repository = cursors.SqlJobCursorRepository(factory)
    first_updated_at = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    second_updated_at = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        current = first_updated_at

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(cursors, "datetime", FrozenDateTime)

    repository.save("daily:600519.SH", {"page": 1})
    FrozenDateTime.current = second_updated_at
    repository.save("daily:600519.SH", {"page": 2})

    with factory() as session:
        rows = session.scalars(select(JobCursor)).all()

    assert len(rows) == 1
    assert rows[0].job_name == "backfill"
    assert rows[0].cursor_key == "daily:600519.SH"
    assert rows[0].cursor_value == {"page": 2}
    persisted_updated_at = rows[0].updated_at.replace(tzinfo=timezone.utc)
    assert persisted_updated_at == second_updated_at
    assert persisted_updated_at > first_updated_at
    engine.dispose()


def test_separate_job_names_do_not_collide() -> None:
    from app.jobs.cursors import SqlJobCursorRepository

    engine, factory = session_factory()
    backfill = SqlJobCursorRepository(factory, job_name="backfill")
    incremental = SqlJobCursorRepository(factory, job_name="incremental")

    backfill.save("daily:600519.SH", {"page": 1})
    incremental.save("daily:600519.SH", {"page": 8})

    assert backfill.load("daily:600519.SH") == {"page": 1}
    assert incremental.load("daily:600519.SH") == {"page": 8}
    engine.dispose()


def test_delete_removes_only_exact_row() -> None:
    from app.jobs.cursors import SqlJobCursorRepository

    engine, factory = session_factory()
    backfill = SqlJobCursorRepository(factory, job_name="backfill")
    incremental = SqlJobCursorRepository(factory, job_name="incremental")

    backfill.save("daily:600519.SH", {"page": 1})
    backfill.save("daily:000001.SZ", {"page": 2})
    incremental.save("daily:600519.SH", {"page": 3})

    backfill.delete("daily:600519.SH")

    assert backfill.load("daily:600519.SH") is None
    assert backfill.load("daily:000001.SZ") == {"page": 2}
    assert incremental.load("daily:600519.SH") == {"page": 3}
    engine.dispose()
