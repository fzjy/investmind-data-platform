from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.jobs.run_errors import JobConfigurationError


@contextmanager
def open_readonly_factory(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() == 'postgresql' and url.get_driver_name() == 'psycopg':
        engine = create_engine(url, isolation_level='REPEATABLE READ', connect_args={
            'options': '-c default_transaction_read_only=on -c statement_timeout=30000'})
    else:
        raise JobConfigurationError('unsupported readonly database driver')
    try:
        with engine.connect() as connection, connection.begin():
            yield sessionmaker(bind=connection, expire_on_commit=False)
    finally:
        engine.dispose()
