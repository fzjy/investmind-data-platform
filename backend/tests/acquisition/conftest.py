import pytest

from app.db.base import Base, DerivedBase, MartBase
from app.db.session import create_engine_and_session


@pytest.fixture
def factory(tmp_path):
    engine, factory = create_engine_and_session('sqlite+pysqlite:///' + str(tmp_path/'status.db'))
    for base in (Base, DerivedBase, MartBase):
        base.metadata.create_all(engine)
    yield factory
    engine.dispose()
