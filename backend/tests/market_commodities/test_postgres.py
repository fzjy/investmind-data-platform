"""只使用登记的一次性PG17验证，不连接业务库。"""
import os
import pytest
from sqlalchemy import inspect, select,text
from testcontainers.community.postgres import PostgresContainer

from app.db.base import ALL_BASES
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine,data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from app.shared.readonly_database import open_readonly_factory
from backend.tests.market_commodities.test_sync import setup,NOW
from backend.tests.marts.test_content_runtime import state

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG required')]


def test_pg_real_readonly_atomic_pair_lock_and_checkpoint_reentry(monkeypatch):
    sync,seed,client,calls=setup()
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as postgres:
        url=postgres.get_connection_url(driver='psycopg'); engine,factory=create_engine_and_session(url)
        try:
            with engine.begin() as connection:
                for schema in ('core','raw','ops','derived','mart'): connection.execute(text(f'CREATE SCHEMA {schema}'))
            for base in ALL_BASES: base.metadata.create_all(engine)
            with seed.kw['bind'].connect() as source,engine.begin() as target:
                seed_tables = set(inspect(source).get_table_names())
                for base in ALL_BASES:
                    for table in base.metadata.sorted_tables:
                        if table.name not in seed_tables:
                            continue
                        rows=[dict(row) for row in source.execute(select(table)).mappings()]
                        if rows:
                            target.execute(table.insert(),rows)
                            if 'id' in table.c and str(table.c.id.type)=='INTEGER':
                                target.execute(text("SELECT setval(pg_get_serial_sequence(:t,'id'),:n,true)"),{'t':table.fullname,'n':max(r['id'] for r in rows)})
            sync.factory,sync.lock=factory,job_lock_for_engine(engine)
            with open_readonly_factory(url) as readonly:
                sync.factory=readonly
                assert sync.preflight(NOW)['pendingCount']==24 and calls==[]
                sync.factory=factory
            before=state(factory); original=SnapshotPublisher._write_page_snapshot
            def fail(self,session,candidate,version):
                if candidate.product=='market-explorer': raise RuntimeError('controlled rollback')
                return original(self,session,candidate,version)
            monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',fail)
            with client: report=sync.run(NOW,publish=True)
            assert report['requestCount']==44 and report['pendingCount']==0 and report['status']=='partial'
            assert state(factory)==before
            monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',original)
            assert sync.lock.try_acquire(data_publication_lock_key())
            try: assert sync.run(NOW,publish=True)['publication']['code']=='COMMODITY_PUBLICATION_LOCK_BUSY'
            finally: sync.lock.release(data_publication_lock_key())
            assert sync.run(NOW,publish=True)['publication']['status']=='completed'
            after=state(factory)
            for product in ('market-overview','market-explorer'):
                missing=after['pointers'][product][2]['missingData']
                assert {'source':'commodities','reason':'尚未采集真实来源'} not in missing
                assert {'source':'commodities:overseas','reason':'海外商品真实来源未接入'} in missing
                assert {'source':'commodities:intraday','reason':'日内商品真实来源未接入'} in missing
                assert all(row in missing for row in before['pointers'][product][2]['missingData'] if row['source']!='commodities')
            assert sync.run(NOW,publish=True)['publication']['status']=='no_change'
            assert state(factory)==after and len(calls)==44
        finally:
            client.close(); engine.dispose(); seed.kw['bind'].dispose()
