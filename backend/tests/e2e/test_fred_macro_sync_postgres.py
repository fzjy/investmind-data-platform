"""一次性 PostgreSQL17 核验 FRED 六项提交、修订有效期与跨连接锁。"""
import os

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import MacroObservation
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_fred_macro_sync import IDS, NOW, KEY, ResponseSource, execute, module, preserved, seed
from backend.tests.jobs.test_fred_macro_sync import test_base_revision_closes_original_interval_without_changing_value_and_adds_new_vintage as check_revision


pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
                      reason='requires disposable PostgreSQL17')]


def prepared(database):
    original, factory, engine = database
    seed(factory)
    return Settings(database_url=original.database_url, fred_api_key=KEY), factory, engine


def test_pg_success_repeat_keeps_publication_and_old_evidence(database):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 6 and report.publication is None
    assert execute(settings, ResponseSource()).row_count == 0
    assert preserved(factory) == before
    with factory() as session: assert len(session.scalars(select(MacroObservation)).all()) == 12


def test_pg_core_commit_interruption_recovers_in_new_runtime(database, monkeypatch):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    with module().open_fred_macro_sync_runtime(settings, transport=httpx.MockTransport(ResponseSource().handle)) as runtime:
        runtime.job._clock = lambda: NOW
        def interrupted(*args, **kwargs): raise KeyboardInterrupt('controlled core-before-state interruption')
        monkeypatch.setattr(runtime.job._states, 'mark_success', interrupted)
        with pytest.raises(KeyboardInterrupt):
            runtime.job.run(MacroSyncOptions(NOW, settings.macro_history_years,
                runtime.catalog.catalog_version, runtime.catalog.catalog_hash,
                force=True, mode='live', series_ids=IDS, publish=False))
    with factory() as session: assert len(session.scalars(select(MacroObservation)).all()) == 7
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 5
    assert preserved(factory) == before


def test_pg_vintage_revision_preserves_value_and_closes_only_interval(database):
    check_revision(prepared(database))


def test_pg_shared_lock_blocks_all_http_and_releases_after_exit(database):
    settings, factory, engine = prepared(database)
    before = preserved(factory)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try: assert execute(settings, source).status == 'locked'
    finally: lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before
    assert execute(settings, source).status == 'completed'
