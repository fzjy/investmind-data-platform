from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import JobRun
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.jobs.build_history import SqlBuildHistory


def _factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
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
    return sessionmaker(engine, expire_on_commit=False)


def test_long_symbol_scope_uses_bounded_deterministic_business_key() -> None:
    factory = _factory()
    history = SqlBuildHistory(factory)
    symbols = tuple(f"{index:06d}.SZ" for index in range(1, 51))
    changed_symbols = (*symbols[:-1], "600519.SH")

    first_id = history.start(BuildAndPublishOptions(date(2026, 9, 18), symbols))
    reordered_id = history.start(
        BuildAndPublishOptions(date(2026, 9, 18), tuple(reversed(symbols)))
    )
    changed_id = history.start(
        BuildAndPublishOptions(date(2026, 9, 18), changed_symbols)
    )

    with factory() as session:
        keys = {
            run.id: run.business_key
            for run in session.scalars(
                select(JobRun).where(
                    JobRun.id.in_((first_id, reordered_id, changed_id))
                )
            )
        }

    assert len(keys[first_id]) <= 256
    assert keys[first_id] == keys[reordered_id]
    assert keys[first_id] != keys[changed_id]
    assert keys[first_id].startswith("2026-09-18:symbols-50:")
