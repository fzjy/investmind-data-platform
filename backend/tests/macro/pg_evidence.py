"""在单个隔离事务中构造已损坏或特定历史时间的证据。"""
from contextlib import contextmanager

from sqlalchemy import text


@contextmanager
def evidence_fixture_transaction(factory, *models):
    with factory.begin() as session:
        triggers = []
        for model in models:
            table = model.__table__.fullname
            trigger = session.execute(text("""
                SELECT tgname FROM pg_trigger
                WHERE tgrelid = CAST(:table AS regclass)
                  AND NOT tgisinternal AND tgenabled = 'O'
                  AND starts_with(tgname, 'immutable_enterprise_')
            """), {"table": table}).scalar_one()
            session.connection().exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            triggers.append((table, trigger))
        yield session
        session.flush()
        for table, trigger in triggers:
            session.connection().exec_driver_sql(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
