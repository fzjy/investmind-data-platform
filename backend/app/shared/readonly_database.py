from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.jobs.run_errors import JobConfigurationError


@contextmanager
def open_readonly_factory(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() == 'sqlite':
        if not url.database or url.database == ':memory:' or not Path(url.database).is_file():
            raise JobConfigurationError('existing SQLite database required')
        url = url.set(database=Path(url.database).resolve().as_uri(),
            query={**url.query, 'uri': 'true', 'mode': 'ro'})
        engine = create_engine(url, execution_options={'schema_translate_map': {
            'raw': None, 'core': None, 'ops': None, 'derived': None, 'mart': None}})
        @event.listens_for(engine, 'connect')
        def readonly(dbapi_connection, _record):
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute('PRAGMA query_only=ON')
            finally:
                cursor.close()
        @event.listens_for(engine, 'begin')
        def snapshot(connection):
            connection.exec_driver_sql('BEGIN')
    elif url.get_backend_name() == 'postgresql' and url.get_driver_name() == 'psycopg':
        engine = create_engine(url, isolation_level='REPEATABLE READ', connect_args={
            'options': '-c default_transaction_read_only=on -c statement_timeout=30000'})
    else:
        raise JobConfigurationError('unsupported readonly database driver')
    try:
        with engine.connect() as connection, connection.begin():
            yield sessionmaker(bind=connection, expire_on_commit=False)
    finally:
        engine.dispose()
