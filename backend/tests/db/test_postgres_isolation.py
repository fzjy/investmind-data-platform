from sqlalchemy import create_engine, text


def test_databases_are_independent(pg_database):
    first = create_engine(pg_database())
    second = create_engine(pg_database())
    try:
        with first.begin() as connection:
            assert connection.scalar(text('select 1')) == 1
            connection.execute(text('create table isolation_probe (id integer)'))
        with second.connect() as connection:
            assert connection.scalar(text("select to_regclass('public.isolation_probe')")) is None
    finally:
        first.dispose()
        second.dispose()
