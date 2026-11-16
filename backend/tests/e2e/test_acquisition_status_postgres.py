import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import PROJECT_ROOT, Settings
from app.acquisition.contracts import Attempt, Evidence, StateKey
from app.acquisition.repository import StateRepository

pytestmark = pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='仅登记的一次性 PostgreSQL')


def test_migration_preserves_pointer_and_newer_concurrent_attempt(monkeypatch):
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as pg:
        url = pg.get_connection_url(driver='psycopg')
        monkeypatch.setattr(config_module,'settings',Settings(_env_file=None,database_url=url))
        cfg = Config(str(PROJECT_ROOT/'backend/alembic.ini'))
        command.upgrade(cfg,'20261115_0021')
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("""INSERT INTO mart.published_snapshot(product,version,as_of,payload,stale)
                                 VALUES ('acceptance','original',now(),'{}',false)"""))
        command.upgrade(cfg,'head')
        factory = sessionmaker(engine)
        key = StateKey('stock-dividend','','security','600519.SH')
        start = datetime.now(UTC)
        barrier = Barrier(2)
        def worker(i):
            attempt = Attempt(key,str(i),start+timedelta(seconds=i),{},'test')
            barrier.wait(timeout=10)
            with factory.begin() as session:
                repo = StateRepository(session)
                if repo.begin(attempt):
                    repo.finish(attempt,Evidence('failed',attempt.started_at,None,'FAILED_'+str(i),'',{},()))
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(worker,(1,2)))
        with factory() as session:
            assert StateRepository(session).read((key,))[key].error_code == 'FAILED_2'
            assert session.scalar(text("SELECT version FROM mart.published_snapshot WHERE product='acceptance'")) == 'original'
        engine.dispose()
