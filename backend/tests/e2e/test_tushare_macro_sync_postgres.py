"""一次性 PG17 验证六项 Tushare 宏观增量、提交中断恢复、跨连接锁。"""
import os

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import MacroObservation, PublishedSnapshot
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_tushare_macro_sync import IDS, NOW, TOKEN, ResponseSource, execute, module, preserved, seed


pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
                      reason='requires disposable PostgreSQL17')]


def prepared(database):
    original, factory, engine = database
    seed(factory)
    settings = Settings(database_url=original.database_url, tushare_token=TOKEN,
                        tushare_standard_requests_per_minute=600000)
    return settings, factory, engine


def test_pg_success_repeat_keeps_publication_and_old_evidence(database):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 5 and report.publication is None
    assert execute(settings, ResponseSource()).row_count == 0
    assert preserved(factory) == before
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 11
        assert {r.version for r in session.scalars(select(PublishedSnapshot))} == {'old-published'}


def test_pg_core_commit_interruption_recovers_in_new_runtime(database, monkeypatch):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    with module().open_tushare_macro_sync_runtime(settings, transport=httpx.MockTransport(ResponseSource().handle)) as runtime:
        runtime.job._clock = lambda: NOW
        original = runtime.job._states.mark_success
        def interrupted(*args, **kwargs):
            if kwargs.get('series_id') == IDS[1] or (args and args[0] == IDS[1]):
                raise KeyboardInterrupt('controlled core-before-state interruption')
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime.job._states, 'mark_success', interrupted)
        with pytest.raises(KeyboardInterrupt):
            runtime.job.run(MacroSyncOptions(NOW, settings.macro_history_years,
                runtime.catalog.catalog_version, runtime.catalog.catalog_hash,
                force=True, mode='live', series_ids=IDS, publish=False))
    with factory() as session: assert len(session.scalars(select(MacroObservation)).all()) == 7
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 4
    assert preserved(factory) == before


def test_pg_shared_lock_blocks_http_and_releases_after_exit(database):
    settings, factory, engine = prepared(database)
    before = preserved(factory)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try: assert execute(settings, source).status == 'locked'
    finally: lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before
    assert execute(settings, source).status == 'completed'
