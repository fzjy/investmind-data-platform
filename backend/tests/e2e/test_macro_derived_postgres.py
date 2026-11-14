"""UI-017 derived pair atomicity in a registered disposable PG17 only."""
from datetime import timedelta
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import func, select, text
from testcontainers.community.postgres import PostgresContainer

from app.db.models import MacroObservation, PageSnapshot, PublishedSnapshot
from app.db.session import create_engine_and_session
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_sync import MacroSeriesOutcome
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from backend.tests.marts.test_macro_derivations import CATALOG, CUTOFF, row

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG17 only')]
ROOT = Path(__file__).resolve().parents[3]


def test_pg_derived_pair_rolls_back_second_insert_and_reuses_same_input(monkeypatch):
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        try:
            with engine.connect() as connection:
                assert int(connection.scalar(text('SHOW server_version_num'))) // 10000 == 17
            with factory.begin() as session:
                session.add_all(row(item.id, '2026-08-01', 1) for item in CATALOG.series)
            history = SqlMacroSyncHistory(factory)
            publisher = SnapshotPublisher(factory)
            job = MacroPublishJob(MacroMartBuilder(MacroRepository(factory), catalog=CATALOG), publisher, history)
            statuses = tuple(MacroSeriesOutcome(item.id, 'fresh', item.sources[0].provider, 0, 0, 0, ()) for item in CATALOG.series)

            def publish(seconds):
                checked = CUTOFF + timedelta(seconds=seconds)
                run_id = history.start(f'derived:{seconds}', checked, CATALOG.catalog_version, CATALOG.catalog_hash, 'test', 1)
                return job.publish(run_id, checked, CATALOG, statuses, reuse_unchanged=True)

            def snapshots():
                with factory() as session:
                    pointers = [(p.product, p.version, p.as_of, p.published_at, p.payload) for p in session.scalars(select(PublishedSnapshot).order_by(PublishedSnapshot.product))]
                    count = session.scalar(select(func.count()).select_from(PageSnapshot))
                    return pointers, count

            first = publish(0)
            assert first.status == 'completed' and len(first.promoted_products) == 2
            assert next(q for q in first.quality if q.name == 'macro-derived-valid').details['sourceBound'] is True
            before = snapshots()
            with factory.begin() as session:
                target = session.scalar(select(MacroObservation).where(MacroObservation.series_id == 'cn.money.m1_yoy'))
                target.value = 4
            original = publisher._write_page_snapshot

            def fail_second(session, candidate, version):
                if candidate.product == 'macro-insights':
                    raise RuntimeError('controlled second insert failure')
                return original(session, candidate, version)

            monkeypatch.setattr(publisher, '_write_page_snapshot', fail_second)
            with pytest.raises(RuntimeError, match='controlled second insert'):
                publish(1)
            assert snapshots() == before
            monkeypatch.setattr(publisher, '_write_page_snapshot', original)
            recovered = publish(2)
            assert recovered.version != first.version and len(recovered.promoted_products) == 2
            after = snapshots()
            repeated = publish(3)
            assert repeated.version == recovered.version and repeated.promoted_products == ()
            assert snapshots() == after
            lock = PostgresAdvisoryJobLock(engine)
            other = PostgresAdvisoryJobLock(engine)
            assert lock.try_acquire(data_publication_lock_key())
            try:
                assert not other.try_acquire(data_publication_lock_key())
            finally:
                lock.release(data_publication_lock_key())
        finally:
            engine.dispose()
