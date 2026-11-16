"""登记一次性PG17验证实际只读、事务与跨连接锁；不连接业务库。"""
import os

import pytest

from app.jobs.locks import job_lock_for_engine, data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from app.market.etf.repository import EtfRepository
from app.shared.readonly_database import open_readonly_factory
from backend.tests.market_etf.test_etf import setup, NOW, POOL
from backend.tests.market_etf.test_publication import prepare
from backend.tests.marts.test_content_runtime import state

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG required')]


def test_pg_readonly_checkpoints_atomic_pair_and_advisory_lock(new_pg_factory, monkeypatch):
    sync,factory,client,calls,_=setup(new_pg_factory)
    prepare(factory)
    engine = factory.kw["bind"]
    url = engine.url.render_as_string(hide_password=False)
    try:
        sync.factory,sync.lock=factory,job_lock_for_engine(engine)
        with open_readonly_factory(url) as readonly:
            original=sync.factory
            sync.factory=readonly
            assert sync.preflight(NOW)['pendingCount']==18
            sync.factory=original
        before=state(factory)
        original_write=SnapshotPublisher._write_page_snapshot
        def fail(self,session,candidate,version):
            if candidate.product=='market-explorer': raise RuntimeError('controlled rollback')
            return original_write(self,session,candidate,version)
        monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',fail)
        with client: report=sync.run(NOW,publish=True)
        assert report['requestCount']==18 and report['status']=='partial' and len(calls)==18
        assert state(factory)==before
        monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',original_write)
        assert sync.lock.try_acquire(data_publication_lock_key())
        try: assert sync.run(NOW,publish=True)['publication']['code']=='ETF_PUBLICATION_LOCK_BUSY'
        finally: sync.lock.release(data_publication_lock_key())
        assert sync.run(NOW,publish=True)['publication']['status']=='completed'
        after=state(factory)
        assert sync.run(NOW,publish=True)['publication']['status']=='no_change'
        assert state(factory)==after and len(calls)==18
    finally:
        client.close()
