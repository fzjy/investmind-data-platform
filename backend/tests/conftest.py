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

@pytest.fixture(scope="session")
def pg_template(pg_instance_url):
    """只迁移一次，随后每个用例克隆完整 schema、约束及触发器。"""
    from alembic import command
    from alembic.config import Config
    from app.config import PROJECT_ROOT
    with postgres.database(pg_instance_url) as template:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DANG_DATABASE_URL", template)
            command.upgrade(Config(str(PROJECT_ROOT / "backend/alembic.ini")), "head")
        yield template


@pytest.fixture
def pg_schema_url(pg_instance_url, pg_template):
    with postgres.database(pg_instance_url, template=pg_template) as url:
        yield url


@pytest.fixture
def new_pg_factory(pg_instance_url, pg_template):
    """需要多个独立目标的用例可重复创建完整数据库。"""
    from contextlib import ExitStack
    from app.db.session import create_engine_and_session

    with ExitStack() as stack:
        def create():
            url = stack.enter_context(postgres.database(pg_instance_url, template=pg_template))
            engine, factory = create_engine_and_session(url)
            stack.callback(engine.dispose)
            return factory

        yield create


@pytest.fixture
def pg_factory(pg_schema_url):
    from app.db.session import create_engine_and_session
    engine, factory = create_engine_and_session(pg_schema_url)
    try:
        yield factory
    finally:
        engine.dispose()


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
