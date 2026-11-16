import pytest
from sqlalchemy.schema import CreateSchema

from app.db.base import ALL_BASES
from app.db.session import create_engine_and_session


@pytest.fixture
def factory(pg_url):
    engine, factory = create_engine_and_session(pg_url)
    with engine.begin() as connection:
        for schema in {table.schema for base in ALL_BASES for table in base.metadata.tables.values() if table.schema}:
            connection.execute(CreateSchema(schema, if_not_exists=True))
    for base in ALL_BASES:
        base.metadata.create_all(engine)
    yield factory
    engine.dispose()
