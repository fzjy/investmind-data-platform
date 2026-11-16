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

@pytest.fixture
def control_store(pg_url):
    from app.db.models.local_admin import LocalAdminTask, LocalAdminExecution
    from app.db.session import create_engine_and_session
    from app.local_admin.control import ControlStore
    engine, factory = create_engine_and_session(pg_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA ops")
        for model in (LocalAdminTask, LocalAdminExecution):
            model.__table__.create(connection)
    store = ControlStore(factory)
    store.acquire()
    try:
        yield store
    finally:
        store.release()
        engine.dispose()
