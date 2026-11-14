from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import create_engine_and_session, transaction


def test_transaction_rolls_back_on_error() -> None:
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("create table rollback_probe (id integer primary key)"))

    with pytest.raises(RuntimeError), transaction(factory) as session:
        session.execute(text("insert into rollback_probe (id) values (1)"))
        raise RuntimeError("stop")

    with factory() as session:
        count = session.scalar(text("select count(*) from rollback_probe"))

    assert count == 0
    engine.dispose()
