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


child_url = None


def run_child(url, module, *args):
    """仅传递已登记用例库，凭据通过 stdin 进入隔离入口。"""
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    parsed = make_url(url)
    identity = (parsed.host, parsed.port, parsed.database, parsed.username, parsed.password)
    if not enabled or identity not in allowed_databases or not parsed.database.startswith('test_'):
        raise AssertionError('Only registered case databases may be passed to children')
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(('DANG_', 'COMPOSE_', 'PG'))}
    return subprocess.run(
        [sys.executable, '-I', str(root / 'backend/tests/run_isolated.py'),
         '--registered-child', module, *args],
        input=json.dumps({'url': url}) + '\n', text=True, capture_output=True,
        cwd=root, env=env, timeout=60,
    )
