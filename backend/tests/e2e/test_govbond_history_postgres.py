"""一次性 PG17 的真实事务、raw/core 和新进程恢复；外部 HTTP 完全受控。"""
import os

import httpx
import pytest
from sqlalchemy import select

from app.db.models import JobCursor, MacroObservation, MacroSeriesState, PublishedSnapshot
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_govbond_history import NOW, Transport

pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='requires disposable PostgreSQL 17')]


def run(runtime):
    runtime.job.clock = lambda: NOW
    return runtime.job.run(as_of=NOW, history_years=1, end=None, request_limit=3)


def test_new_postgres_runtime_resumes_and_leaves_nbs_cursor_untouched(database):
    from app.macro.jobs.govbond_history_runtime import open_govbond_history_runtime, read_govbond_history_status
    from app.jobs.cursors import SqlJobCursorRepository
    settings, factory, _ = database
    SqlJobCursorRepository(factory, job_name='backfill-macro-history').save('nbs-history-v1', {'protected': 'old'})
    first = Transport(rows=16)
    with open_govbond_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=first.post_json(str(r.url), {}).payload))) as runtime:
        assert run(runtime)['rowCount'] == 16
    assert read_govbond_history_status(settings)['completedMonths'] == 1
    second = Transport()
    with open_govbond_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=second.post_json(str(r.url), {}).payload))) as runtime:
        assert run(runtime)['rowCount'] == 1
    assert second.calls[0]['startDate'] == '2026-08-01'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 17
        assert session.scalars(select(MacroSeriesState)).all() == []
        assert session.scalars(select(PublishedSnapshot)).all() == []
        cursor = session.scalar(select(JobCursor).where(JobCursor.cursor_key == 'nbs-history-v1'))
        assert cursor.cursor_value == {'protected': 'old'}


def test_postgres_core_commit_survives_interrupt_before_done(database, monkeypatch):
    from app.macro.jobs.govbond_history_runtime import open_govbond_history_runtime
    settings, factory, _ = database
    transport = Transport()
    with open_govbond_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=transport.post_json(str(r.url), {}).payload))) as runtime:
        original = runtime.job.cursors.save
        def interrupt(key, value):
            if any(e['status'] == 'done' for e in value['entries']):
                raise KeyboardInterrupt()
            original(key, value)
        monkeypatch.setattr(runtime.job.cursors, 'save', interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(runtime)
    with open_govbond_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=transport.post_json(str(r.url), {}).payload))) as runtime:
        result = run(runtime)
        assert result['rowCount'] == 0 and result['completedMonths'] == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1
