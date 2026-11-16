"""一次性实例与逐用例数据库；登记表仅由隔离入口持有。"""
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

allowed_databases = set()
enabled = False


@contextmanager
def database(instance_url, *, template=None):
    name = 'test_' + uuid4().hex
    admin = create_engine(instance_url, isolation_level='AUTOCOMMIT')
    url = make_url(instance_url).set(database=name)
    identity = (url.host, url.port, url.database, url.username, url.password)
    source = ""
    if template:
        template_url = make_url(template)
        if template_url.set(database=name) != url or not template_url.database.startswith("test_"):
            raise ValueError("expected a template from this disposable instance")
        source = " TEMPLATE " + admin.dialect.identifier_preparer.quote_identifier(template_url.database)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"{source}'))

    allowed_databases.add(identity)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        allowed_databases.discard(identity)
        admin.dispose()


child_url = None


def _child_worker(url, module, args, output):
    import os
    for name in tuple(os.environ):
        if name.upper().startswith("PG"):
            del os.environ[name]
    import contextlib
    import io
    import sys
    from backend.tests.run_isolated import main
    stdout, stderr = io.StringIO(), io.StringIO()
    sys.argv = ['run_isolated.py', module, *args]
    code = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main(child_url=url)
        except SystemExit as error:
            code = error.code or 0
        except BaseException:
            import traceback
            traceback.print_exc()
            code = 1
    output.send((code, stdout.getvalue(), stderr.getvalue()))
    output.close()


def run_child(url, module, *args):
    """独立解释器只从已验证父进程接收用例身份，无公共 URL 输入入口。"""
    import multiprocessing
    from types import SimpleNamespace
    parsed = make_url(url)
    identity = (parsed.host, parsed.port, parsed.database, parsed.username, parsed.password)
    if not enabled or identity not in allowed_databases or not parsed.database.startswith('test_'):
        raise AssertionError('Only registered case databases may be passed to children')
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_child_worker, args=(url, module, args, send))
    process.start()
    send.close()
    try:
        if not receive.poll(60):
            raise TimeoutError('Isolated child timed out')
        code, stdout, stderr = receive.recv()
        process.join(10)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    finally:
        if process.is_alive():
            process.terminate()
        process.join()
        receive.close()
