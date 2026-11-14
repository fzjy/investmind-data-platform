"""复用登记一次性PG17；真实SQL事务/约束，不连接业务库。"""
import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import MarketDailyQuote, SourcePayload
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from backend.tests.market_data.test_sync import job
from backend.tests.market_data.test_publication import publish
from backend.tests.market_data.test_repository import NOW
from backend.tests.marts.test_content_runtime import state
from backend.tests.marts.test_content_runtime_postgres import database

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG required')]


def ingest(factory):
    sync, client, requests = job(factory)
    try:
        assert sync.run(NOW).request_count == 15 and len(requests) == 15
        assert sync.run(NOW).request_count == 0
    finally:
        client.close()


def test_pg_ingestion_publication_constraints_and_non_target_protection(database):
    _, factory, engine = database
    before = state(factory)
    ingest(factory)
    assert publish(factory)['status'] == 'completed'
    after = state(factory)
    assert len(after['pointers']['market-overview'][2]['globalIndices']) == 6
    assert len(after['pointers']['market-overview'][2]['etfQuotes']) == 9
    assert after['workbenches'] == before['workbenches']
    for product, original in before['pointers'].items():
        if product not in {'market-overview','market-explorer'}:
            assert after['pointers'][product] == original
    assert publish(factory)['status'] == 'no_change' and state(factory) == after
    with factory() as session:
        row = session.scalar(select(MarketDailyQuote))
        attrs = {column.name:getattr(row,column.name) for column in MarketDailyQuote.__table__.columns if column.name != 'id'}
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(MarketDailyQuote(**attrs))
    attrs['asset_code'] = 'controlled-fk-check'
    attrs['source_payload_id'] = 2147483647
    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(MarketDailyQuote(**attrs))


def test_pg_pair_rollback_and_distinct_connection_publication_lock(database, monkeypatch):
    _, factory, engine = database
    ingest(factory)
    before = state(factory)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert publish(factory)['status'] == 'locked'
        assert state(factory) == before
    finally:
        lock.release(data_publication_lock_key())
    original = SnapshotPublisher._write_page_snapshot
    def fail(self, session, candidate, version):
        if candidate.product == 'market-explorer':
            raise RuntimeError('controlled rollback')
        return original(self,session,candidate,version)
    monkeypatch.setattr(SnapshotPublisher,'_write_page_snapshot',fail)
    with pytest.raises(RuntimeError,match='controlled rollback'):
        publish(factory)
    assert state(factory) == before
