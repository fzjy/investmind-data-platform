"""一次性实例与逐用例数据库；登记表仅由隔离入口持有。"""
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

allowed_databases = set()
enabled = False


@contextmanager
def database(instance_url):
    name = 'test_' + uuid4().hex
    admin = create_engine(instance_url, isolation_level='AUTOCOMMIT')
    url = make_url(instance_url).set(database=name)
    identity = (url.host, url.port, url.database, url.username, url.password)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    allowed_databases.add(identity)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        allowed_databases.discard(identity)
        admin.dispose()
