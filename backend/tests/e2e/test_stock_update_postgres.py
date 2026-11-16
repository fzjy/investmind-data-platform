"""一次性登记PG：真实事务、断点恢复、锁及股票51产品发布。"""
from datetime import date
from datetime import datetime, timezone
import os
import json
import pytest
import httpx
from sqlalchemy import create_engine, text, select, event, func
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer
from app.db.base import Base, DerivedBase, MartBase, ExternalBase
from app.db.models import Security, Classification, ClassificationMember, PublishedSnapshot, SourcePayload, JobCursor, DailyPrice
from app.jobs.locks import PostgresAdvisoryJobLock
from app.connectors.tushare.client import TushareClient
from app.stocks.update.planning import StockUpdateOptions
from app.stocks.update.sync import StockUpdateSync
from app.stocks.update.publication import publish_stock_update
from app.stocks.update.repository import StockUpdateRepository
from app.marts.publication import SnapshotPublisher
from app.marts.contracts import MartCandidate
from app.stocks.universes import UniverseResolver
from backend.tests.stock_update.test_sync import mock_client, NOW
from backend.tests.stock_update.test_repository import sample

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='requires registered disposable PostgreSQL17')]


@pytest.fixture(scope='module')
def pg():
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as container:
        engine = create_engine(container.get_connection_url(driver='psycopg'))
        with engine.begin() as c:
            for schema in ('raw', 'core', 'ops', 'derived', 'mart'):
                c.execute(text(f'CREATE SCHEMA {schema}'))
        for base in (Base, DerivedBase, MartBase, ExternalBase):
            base.metadata.create_all(engine)
        try:
            yield engine, sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


def test_pg_unit_commit_failure_retries_without_lost_cursor(pg):
    engine, f = pg
    p, r = sample()
    def fail(s):
        raise RuntimeError('commit fault')
    event.listen(f, 'before_commit', fail)
    try:
        with pytest.raises(RuntimeError):
            StockUpdateRepository(f).ingest_unit('rollback', p, r, date(2026, 10, 13), date(2023, 10, 13))
    finally:
        event.remove(f, 'before_commit', fail)
    with f() as s:
        assert s.scalar(select(func.count()).select_from(SourcePayload)) == 0
        assert s.scalar(select(func.count()).select_from(JobCursor)) == 0
        assert s.scalar(select(func.count()).select_from(DailyPrice)) == 0


def test_pg_source_lock_is_exclusive_and_recoverable(pg):
    engine, _ = pg
    a, b = PostgresAdvisoryJobLock(engine), PostgresAdvisoryJobLock(engine)
    key = 'sync-stock-data:representative-50'
    assert a.try_acquire(key)
    try:
        assert not b.try_acquire(key)
    finally:
        a.release(key)
    assert b.try_acquire(key)
    b.release(key)


def test_pg_resume_and_stock_only_atomic_publication(pg, monkeypatch):
    engine, f = pg
    pool = UniverseResolver().resolve('representative-50')
    with f.begin() as s:
        s.add_all([Security(ts_code=c, symbol=c[:6], name='样本', exchange='SSE', status='L',
            list_date=date(2026, 10, 12)) for c in pool.symbols])
        s.flush()
        nodes = [Classification(provider='tushare', source='SW2021', classification_id=f'test{level}',
            name=f'层级{level}', level=level, parent_id=f'test{level-1}' if level>1 else None, data={}) for level in (1, 2, 3)]
        s.add_all(nodes)
        s.flush()
        s.add_all([ClassificationMember(classification_id=nodes[-1].id, ts_code=c, effective_from=date(2023, 1, 1), data={}) for c in pool.symbols])
    # Calendar transport is a real client with mock external HTTP; all DB/derived/publisher paths remain real.
    def override(body, count):
        if body['api_name'] in ('daily', 'daily_basic', 'adj_factor'):
            fields = body['fields'].split(',')
            rows = [{'ts_code': body['params']['ts_code'], 'trade_date': day,
                'open': 10, 'high': 11, 'low': 9, 'close': 10, 'pre_close': 10, 'vol': 100,
                'pct_chg': 0, 'amount': 1000, 'total_mv': 10000, 'circ_mv': 9000, 'adj_factor': 1,
                'pe': 20, 'pb': 1, 'turnover_rate': 1} for day in ('20261012', '20261013')]
            return httpx.Response(200, json={'code': 0, 'data': {'fields': fields, 'items': [[r.get(field) for field in fields] for r in rows]}})
    client, calls = mock_client(override=override)
    try:
        lock = PostgresAdvisoryJobLock(engine)
        first = StockUpdateSync(f, client, pool, StockUpdateOptions(max_requests=3), lock).run(NOW)
        assert first['status'] == 'partial' and first['requests'] == 3
        job = StockUpdateSync(f, client, pool, StockUpdateOptions(), lock)
        result = job.run(NOW)
        assert result['status'] == 'completed' and result['publishReady'], result
        assert len(calls) == 252
        publisher = SnapshotPublisher(f)
        old = tuple(MartCandidate(product, datetime(2026, 10, 9, tzinfo=timezone.utc),
            {'stocks': [{'symbol': code} for code in pool.symbols]} if product == 'screener' else {'old': True})
            for product in ('screener', *(f'stock-workbench:{c}' for c in pool.symbols), 'market-overview'))
        publisher.publish_many(old, 'old-version', ())
        original = SnapshotPublisher._switch_pointer
        seen = []
        def fail_half(session, candidate, version):
            seen.append(candidate.product)
            outcome = original(session, candidate, version)
            if len(seen) == 26:
                raise RuntimeError('mid-release fault')
            return outcome
        with monkeypatch.context() as scoped:
            scoped.setattr(SnapshotPublisher, '_switch_pointer', staticmethod(fail_half))
            failed = publish_stock_update(f, pool, date(2026, 10, 13), lock, collection=result)
        assert failed.status == 'failed' and len(seen) == 26, (failed.error, seen)
        with f() as s:
            assert {r.version for r in s.scalars(select(PublishedSnapshot))} == {'old-version'}
        report = publish_stock_update(f, pool, date(2026, 10, 13), lock, collection=result)
        assert report.status == 'completed', report
        assert len(report.promoted_products) == 51
        with f() as s:
            assert s.get(PublishedSnapshot, 'market-overview').version == 'old-version'
            pointers = s.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product != 'market-overview')).all()
            assert len(pointers) == 51 and {r.version for r in pointers} == {report.version}
            assert {r.as_of.date() for r in pointers} == {date(2026, 10, 13)}
        resumed = job.run(NOW)
        assert resumed['requests'] == 0 and resumed['publishReady'] is True
    finally:
        client.close()
