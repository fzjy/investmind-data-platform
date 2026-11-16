import pytest


@pytest.fixture
def factory(pg_factory):
    return pg_factory


@pytest.fixture
def stock_factory(factory):
    from app.db.models import Security
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))
    return factory
