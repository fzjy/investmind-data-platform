"""一次性 PG17 的真实事务、raw/core 和新进程恢复；外部 HTTP 完全受控。"""
import os
from datetime import timedelta, timezone, date

import httpx
import pytest
from sqlalchemy import select

from app.db.models import JobCursor, JobFailure, JobRun, IngestionBatch, SourcePayload, MacroObservation, MacroSeriesState, PublishedSnapshot
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_usdcny_history import NOW, Transport

pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='requires disposable PostgreSQL 17')]


def run(runtime):
    runtime.job.clock = lambda: NOW
    return runtime.job.run(as_of=NOW, history_years=1, end=None, request_limit=4)


def test_new_postgres_runtime_resumes_and_leaves_old_data_untouched(database):
    from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime, read_usdcny_history_status
    from app.jobs.cursors import SqlJobCursorRepository
    settings, factory, _ = database
    # 真实受控旧国债raw/core/JobRun，加三份旧cursor及非空state/failure/快照。
    from app.macro.jobs.govbond_history_runtime import open_govbond_history_runtime
    from backend.tests.jobs.test_govbond_history import Transport as GovTransport
    old_transport = GovTransport()
    with open_govbond_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=old_transport.post_json(str(r.url), {}).payload))) as old:
        old.job.clock = lambda: NOW
        assert old.job.run(as_of=NOW, history_years=1, end=None, request_limit=3)['rowCount'] == 1
    for name, key in [('backfill-macro-history', 'nbs-history-v1'),
                      ('backfill-govbond-history', 'govbond-history-v1'),
                      ('backfill-dr007-history', 'dr007-history-v1')]:
        if key != 'govbond-history-v1':
            SqlJobCursorRepository(factory, job_name=name).save(key, {'protected': 'old'})
    with factory.begin() as session:
        session.add(MacroSeriesState(series_id='cn.cpi.core_yoy', catalog_version='protected-v7',
            last_error_code='KEEP', consecutive_failures=3))
        session.add(JobFailure(dataset='macro:cn.cpi.core_yoy', request_key='protected',
            error_code='KEEP', message='old', retryable=True, request_payload={'protected': True}))
        session.add(PublishedSnapshot(product='macro-insights', version='protected-version',
            as_of=NOW, payload={'protected': True}))
    tables = [JobCursor, JobFailure, JobRun, IngestionBatch, SourcePayload,
              MacroObservation, MacroSeriesState, PublishedSnapshot]
    with factory() as session:
        before = {model: [dict(row) for row in session.execute(select(model.__table__)).mappings()]
                  for model in tables}
    first = Transport(rows=11)
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=first.post_json(str(r.url), {}).payload))) as runtime:
        assert run(runtime)['rowCount'] == 11
    assert read_usdcny_history_status(settings)['completedMonths'] == 1
    second = Transport()
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=second.post_json(str(r.url), {}).payload))) as runtime:
        assert run(runtime)['rowCount'] == 1
    assert second.calls[0]['startDate'] == '2026-08-01'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation).where(MacroObservation.series_id == 'cn.fx.usdcny')).all()) == 12
        for model, rows in before.items():
            after = [dict(row) for row in session.execute(select(model.__table__)).mappings()]
            for row in rows:
                assert row in after, f'old {model.__name__} changed'
            if model in (JobFailure, MacroSeriesState, PublishedSnapshot):
                assert after == rows, f'unexpected {model.__name__} writes'


def test_postgres_core_commit_survives_interrupt_before_done(database, monkeypatch):
    from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime
    settings, factory, _ = database
    transport = Transport()
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=transport.post_json(str(r.url), {}).payload))) as runtime:
        original = runtime.job.cursors.save
        def interrupt(key, value):
            if any(e['status'] == 'done' for e in value['entries']):
                raise KeyboardInterrupt()
            original(key, value)
        monkeypatch.setattr(runtime.job.cursors, 'save', interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(runtime)
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=transport.post_json(str(r.url), {}).payload))) as runtime:
        result = run(runtime)
        assert result['rowCount'] == 0 and result['completedMonths'] == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1

def test_postgres_page_commit_survives_interrupt_and_new_runtime_recovers(database):
    from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime, read_usdcny_history_status
    settings, factory, _ = database
    interrupted = Transport(rows=11, failures={('2026-09-01', 2): KeyboardInterrupt()})
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=interrupted.post_json(str(r.url), {}).payload))) as runtime:
        with pytest.raises(KeyboardInterrupt):
            run(runtime)
    with factory() as session:
        assert session.scalars(select(MacroObservation)).all() == []
        raw = session.scalar(select(SourcePayload))
        assert len(raw.payload['data']['pages'][0]['response']['records']) == 10
    assert read_usdcny_history_status(settings)['completedMonths'] == 0
    recovered = Transport(rows=11)
    with open_usdcny_history_runtime(settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=recovered.post_json(str(r.url), {}).payload))) as runtime:
        result = run(runtime)
        assert result['rowCount'] == 11 and result['completedMonths'] == 1
    assert [int(call['pageNum']) for call in recovered.calls] == [1, 2]


def test_postgres_publication_lock_blocks_before_requests_and_all_writes(database):
    from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime
    from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
    settings, factory, engine = database
    contender = PostgresAdvisoryJobLock(engine)
    key = data_publication_lock_key()
    assert contender.try_acquire(key)
    calls = []
    try:
        with open_usdcny_history_runtime(settings,
            transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(503))) as runtime:
            assert run(runtime) == {'status': 'locked', 'requestCount': 0, 'rowCount': 0}
    finally:
        contender.release(key)
    assert calls == []
    with factory() as session:
        for model in (JobCursor, JobRun, IngestionBatch, SourcePayload, MacroObservation,
                      JobFailure, MacroSeriesState, PublishedSnapshot):
            assert session.scalars(select(model)).all() == []


def test_postgres_non_utc_metadata_repair_survives_core_cursor_interrupt(database, monkeypatch):
    from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime, read_usdcny_history_status
    settings, factory, _ = database
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    def execute(runtime, capture):
        runtime.job.clock = lambda: capture
        return runtime.job.run(as_of=capture, history_years=1, end=None, request_limit=100)
    def mock(transport):
        return httpx.MockTransport(lambda r: httpx.Response(200, json=transport.post_json(str(r.url), {}).payload))
    with open_usdcny_history_runtime(settings, transport=mock(Transport())) as runtime:
        assert execute(runtime, local)['status'] == 'completed'
    with factory.begin() as session:
        old = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30)))
        old.unit = 'bp'
        old_id = old.id
    assert read_usdcny_history_status(settings)['completedMonths'] == 11
    with open_usdcny_history_runtime(settings, transport=mock(Transport())) as runtime:
        original = runtime.job.cursors.save
        def interrupt(key, value):
            if all(entry['status'] == 'done' for entry in value['entries']):
                raise KeyboardInterrupt()
            original(key, value)
        monkeypatch.setattr(runtime.job.cursors, 'save', interrupt)
        with pytest.raises(KeyboardInterrupt):
            execute(runtime, local + timedelta(days=1))
    with open_usdcny_history_runtime(settings, transport=mock(Transport())) as runtime:
        result = execute(runtime, local + timedelta(days=2))
        assert result['status'] == 'completed' and result['rowCount'] == 0
    complete = Transport()
    with open_usdcny_history_runtime(settings, transport=mock(complete)) as runtime:
        assert execute(runtime, local + timedelta(days=3))['requestCount'] == 0
    assert complete.calls == []
    with factory() as session:
        assert session.get(MacroObservation, old_id).unit == 'bp'
        rows = session.scalars(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30))
            .order_by(MacroObservation.id)).all()
        assert len(rows) == 2 and rows[1].unit != 'bp' and rows[1].revision.startswith('repair-metadata:')
