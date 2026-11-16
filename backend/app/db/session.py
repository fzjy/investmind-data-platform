from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def create_engine_and_session(
    database_url: str,
    *, connection_options: dict[str, object] | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    if make_url(database_url).get_backend_name() != "postgresql":
        raise ValueError("PostgreSQL database URL required")
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=dict(connection_options or {}))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory.begin() as session:
        yield session
