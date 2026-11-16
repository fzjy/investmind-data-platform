import pytest
from backend.tests.support import postgres


@pytest.fixture(scope='session')
def pg_instance_url():
    if not postgres.enabled:
        pytest.skip('使用 run_isolated.py --postgres 启用隔离数据库')
    with postgres.PostgresContainer('postgres:17-alpine', driver='psycopg') as instance:
        yield instance.get_connection_url()


@pytest.fixture
def pg_database(pg_instance_url):
    """分配空库；同一用例可多次调用，连接由调用者关闭。"""
    from contextlib import ExitStack
    with ExitStack() as stack:
        yield lambda: stack.enter_context(postgres.database(pg_instance_url))


@pytest.fixture
def pg_url(pg_database):
    return pg_database()
