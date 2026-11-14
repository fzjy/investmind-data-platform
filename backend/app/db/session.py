from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_and_session(
    database_url: str,
    *, connection_options: dict[str, object] | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    connect_args: dict[str, object] = dict(connection_options or {})
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine_kwargs: dict[str, object] = {"pool_pre_ping": True, "connect_args": connect_args}
    if database_url.startswith("sqlite"):
        engine_kwargs["execution_options"] = {"schema_translate_map": {"raw": None, "core": None, "ops": None, "derived": None, "mart": None}}
    engine = create_engine(database_url, **engine_kwargs)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory.begin() as session:
        yield session
