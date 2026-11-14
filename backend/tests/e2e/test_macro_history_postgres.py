"""真实 PG17 验证历史 runtime；只允许隔离入口登记的一次性容器。"""
from datetime import date, datetime
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy import event
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

import app.config as config_module
from app.config import Settings
from app.db.models import JobCursor, JobRun, MacroObservation, PublishedSnapshot, MacroSeriesState
from app.db.session import create_engine_and_session
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_history_backfill import CURSOR_KEY
from app.macro.jobs.macro_history_runtime import open_macro_history_runtime, read_macro_history_status
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_history_reconcile import reconcile_macro_history
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import ARCHIVED_V7_FILE
from backend.tests.jobs.test_macro_history_backfill import AS_OF, BODY, INDEX, RELEASE, URL, page
from backend.tests.jobs.test_macro_history_reconcile import seed_complete_history, AS_OF as RECONCILE_AS_OF, IDS


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
                       reason='requires explicitly enabled disposable PostgreSQL 17'),
]
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def database(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, 'PROJECT_ENV_FILE', tmp_path / 'missing.env')
    monkeypatch.chdir(PROJECT_ROOT)
    manifest = tmp_path / 'releases.json'
    manifest.write_text(json.dumps({'schemaVersion': '1.0', 'releases': [RELEASE.model_dump(by_alias=True)]}), encoding='utf-8')
    with PostgresContainer('postgres:17-alpine', driver='psycopg') as postgres:
        url = postgres.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
        monkeypatch.setenv('DANG_DATABASE_URL', url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL', url)
        command.upgrade(Config(str(PROJECT_ROOT / 'backend/alembic.ini')), 'head')
        engine, factory = create_engine_and_session(url)
        settings = Settings(database_url=url, macro_history_years=1,
            macro_backfill_end=date(2026, 8, 31), macro_backfill_request_limit=2,
            macro_backfill_releases_file=manifest)
        try:
            with engine.connect() as connection:
                assert int(connection.scalar(text('SHOW server_version_num'))) // 10000 == 17
            yield settings, factory, engine
        finally:
            engine.dispose()


class Responses:
    def __init__(self, bodies):
        self.bodies, self.calls = bodies, []

    def handle(self, request):
        url = str(request.url)
        self.calls.append(url)
        assert url in self.bodies, f'unexpected controlled URL: {url}'
        return httpx.Response(200, text=self.bodies[url])


def batch(runtime, settings, *, as_of=AS_OF):
    return runtime.job.run(as_of=as_of, history_years=settings.macro_history_years,
        end=settings.macro_backfill_end, request_limit=settings.macro_backfill_request_limit,
        releases=runtime.releases)


def test_history_resumes_with_new_postgres_runtime_without_refetching_done_report(database):
    settings, factory, engine = database
    first = Responses({URL: BODY, INDEX: page(count=2, links=False)})
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(first.handle)) as runtime:
        result = batch(runtime, settings)
    assert result['rowCount'] == 1 and result['requestCount'] == 2
    assert first.calls == [URL, INDEX]
    assert read_macro_history_status(settings)['nextIndexPage'] == 1
    second = Responses({INDEX: page(count=2, links=False), INDEX + 'index_1.html': page(1, 2, False)})
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(second.handle)) as runtime:
        result = batch(runtime, settings, as_of=AS_OF.replace(day=5))
    assert result['rowCount'] == 0 and result['discoveryComplete'] is True
    assert result['asOf'] == '2026-10-01T00:00:00+00:00'
    assert second.calls == [INDEX, INDEX + 'index_1.html']
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert session.scalars(select(MacroSeriesState)).all() == []


def test_postgres_core_commit_then_cursor_interrupt_is_idempotent_after_runtime_close(database, monkeypatch):
    settings, factory, engine = database
    first = Responses({URL: BODY})
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(first.handle)) as runtime:
        original = runtime.job._cursors.save
        def interrupted(key, value):
            if any(item['status'] == 'done' for item in value.get('entries', [])):
                raise KeyboardInterrupt()
            original(key, value)
        monkeypatch.setattr(runtime.job._cursors, 'save', interrupted)
        with pytest.raises(KeyboardInterrupt):
            batch(runtime, settings)
    pending = read_macro_history_status(settings)
    assert pending['pendingReleases'] == 1
    assert '2026-08' in pending['missingMonths']['cn.cpi.core_yoy']
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1
    resumed = Responses({URL: BODY, INDEX: page(links=False)})
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(resumed.handle)) as runtime:
        result = batch(runtime, settings)
    assert result['rowCount'] == 0 and result['pendingReleases'] == 0
    assert '2026-08' not in result['missingMonths']['cn.cpi.core_yoy']
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1


def test_history_postgres_publication_lock_blocks_before_requests_and_cursor(database):
    settings, factory, engine = database
    contender = PostgresAdvisoryJobLock(engine)
    key = data_publication_lock_key()
    assert contender.try_acquire(key)
    source = Responses({URL: BODY, INDEX: page(links=False)})
    try:
        with open_macro_history_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
            assert batch(runtime, settings) == {'status': 'locked', 'requestCount': 0, 'rowCount': 0}
        assert source.calls == []
        with factory() as session:
            assert session.scalars(select(JobCursor).where(JobCursor.cursor_key == CURSOR_KEY)).all() == []
            assert session.scalars(select(MacroObservation)).all() == []
    finally:
        contender.release(key)
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        assert batch(runtime, settings)['rowCount'] == 1


@pytest.mark.parametrize('legacy_hash', [
    '85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31',
    '7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde',
])
def test_verified_catalog_identity_preserves_postgres_checkpoint_and_old_sync_gaps(database, legacy_hash, monkeypatch):
    settings, factory, engine = database
    first = Responses({URL: BODY, INDEX: page(count=2, links=False)})
    from app.macro.jobs import macro_history_runtime as history_runtime
    with monkeypatch.context() as before_upgrade:
        before_upgrade.setattr(history_runtime, 'DEFAULT_MACRO_CATALOG_FILE', ARCHIVED_V7_FILE)
        with open_macro_history_runtime(settings, transport=httpx.MockTransport(first.handle)) as runtime:
            assert batch(runtime, settings)['rowCount'] == 1
    with factory.begin() as session:
        cursor = session.scalar(select(JobCursor).where(JobCursor.cursor_key == CURSOR_KEY))
        before = {**cursor.cursor_value, 'catalog_hash': legacy_hash}
        cursor.cursor_value = before
    catalog = MacroSeriesCatalog.load(ARCHIVED_V7_FILE)
    history = SqlMacroSyncHistory(factory)
    run_id = history.start('old-week', AS_OF, catalog.catalog_version, legacy_hash, 'cli', 1)
    history.finish(run_id, 'partial', {'unresolvedGaps': {
        'cn.cpi.core_yoy': [{'start': '2016-09-30', 'end': '2024-11-30'}]
    }})
    assert history.inspect('new-week', catalog.catalog_version, catalog.catalog_hash) == (False, 1)
    assert history.unresolved_gaps(catalog.catalog_version, catalog.catalog_hash) == {
        'cn.cpi.core_yoy': ((date(2016, 9, 30), date(2024, 11, 30)),)
    }
    resumed = Responses({INDEX: page(count=2, links=False), INDEX + 'index_1.html': page(1, 2, False)})
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(resumed.handle)) as runtime:
        result = batch(runtime, settings, as_of=AS_OF.replace(day=5))
    assert result['rowCount'] == 0 and result['pendingReleases'] == 0
    assert resumed.calls == [INDEX, INDEX + 'index_1.html']
    with factory() as session:
        after = session.scalar(select(JobCursor).where(JobCursor.cursor_key == CURSOR_KEY)).cursor_value
        assert after['catalog_hash'] == legacy_hash
        assert after['entries'] == before['entries'] and after['as_of'] == before['as_of']
        assert session.get(JobRun, run_id).details['catalogHash'] == legacy_hash
        assert len(session.scalars(select(MacroObservation)).all()) == 1
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert session.scalars(select(MacroSeriesState)).all() == []


def test_postgres_reconciliation_is_repeatable_and_explicit_rebuild_publishes_both_products(database, monkeypatch):
    import app.cli.main as cli
    from app.db.models import JobFailure
    from decimal import Decimal
    settings, factory, engine = database
    cursor, old_id, old_details = seed_complete_history(factory)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    # 对目录里其余有来源的序列各放入一条受控观测。
    with factory.begin() as session:
        for definition in catalog.series:
            if definition.id in IDS or not definition.sources:
                continue
            period = date(2026, 4, 1) if definition.frequency == 'quarterly' else date(2026, 8, 1) if definition.frequency == 'monthly' else date(2026, 10, 6)
            released = RECONCILE_AS_OF.replace(day=6)
            session.add(MacroObservation(source=definition.sources[0].provider, series_id=definition.id,
                name=definition.name, region=definition.region, frequency=definition.frequency, unit=definition.unit,
                seasonal_adjustment=definition.seasonal_adjustment, observed_at=period, released_at=released,
                revision='controlled-pg', value=Decimal('1.0'), source_url=str(definition.sources[0].source_url)))
    with factory() as session:
        original_observations = set(session.scalars(select(MacroObservation.id)))
    report = reconcile_macro_history(settings, RECONCILE_AS_OF)
    assert report['status'] == 'completed' and report['published'] is False
    assert reconcile_macro_history(settings, RECONCILE_AS_OF)['status'] == 'no_change'
    with factory() as session:
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert len(session.scalars(select(JobRun)).all()) == 2
    class RebuildClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return RECONCILE_AS_OF.astimezone(tz) if tz else RECONCILE_AS_OF.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", RebuildClock)
    published = cli._run_macro_rebuild(settings)
    assert published.status == 'completed' and set(published.products) == {'macro-insights', 'macro-transmission'}
    with factory() as session:
        assert session.get(JobRun, old_id).details == old_details
        assert session.scalar(select(JobCursor).where(JobCursor.cursor_key == CURSOR_KEY)).cursor_value == cursor
        snapshots = session.scalars(select(PublishedSnapshot)).all()
        assert len(snapshots) == 2 and len({p.version for p in snapshots}) == 1
        for snapshot in snapshots:
            states = {s['id']: s for s in snapshot.payload['seriesStatus']}
            for sid in IDS:
                assert states[sid]['actualSource'] == 'nbs' and states[sid]['staleReason'] is None
                assert states[sid]['observedAt'] == '2026-08-01'
        assert set(session.scalars(select(MacroObservation.id))) == original_observations
        assert all((f.resolved_at is not None) == (f.error_code == 'MACRO_HISTORY_GAP') for f in session.scalars(select(JobFailure)))


def test_postgres_reconcile_lock_leaves_current_gap_and_cursor_intact(database):
    settings, factory, engine = database
    cursor, old_id, old_details = seed_complete_history(factory)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert reconcile_macro_history(settings, RECONCILE_AS_OF)['status'] == 'locked'
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 1
            assert session.scalar(select(JobCursor)).cursor_value == cursor
    finally:
        lock.release(data_publication_lock_key())


def test_postgres_reconcile_commit_failure_rolls_back_and_can_retry(database):
    settings, factory, engine = database
    cursor, old_id, old_details = seed_complete_history(factory)
    def fail_commit(session, context, instances):
        if any(isinstance(item, JobRun) and item.details.get('operation') == 'nbs-history-reconciliation' for item in session.new):
            raise RuntimeError('controlled commit interruption')
    event.listen(Session, 'before_flush', fail_commit)
    try:
        with pytest.raises(RuntimeError, match='controlled commit interruption'):
            reconcile_macro_history(settings, RECONCILE_AS_OF)
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 1
            assert all(session.get(MacroSeriesState, sid).last_error_code == 'MACRO_HISTORY_GAP' for sid in IDS)
            assert session.scalar(select(JobCursor)).cursor_value == cursor
    finally:
        event.remove(Session, 'before_flush', fail_commit)
    assert reconcile_macro_history(settings, RECONCILE_AS_OF)['status'] == 'completed'
