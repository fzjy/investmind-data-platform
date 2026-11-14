"""一次性PG17：v7完成队列→v8当前增量→显式双产品发布。"""
from datetime import date, datetime
import os

import pytest
from sqlalchemy import select

from app.cli.main import _run_macro_rebuild
from app.db.models import JobCursor, JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot
from app.jobs.locks import ProcessLocalJobLock, PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.govbond_history_runtime import read_govbond_history_status
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_govbond_sync import CurrentSource, HISTORY_TIME, NOW, SID, execute, seed

pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='requires disposable PostgreSQL17')]


def prepared(database):
    settings, factory, engine = database
    settings = settings.model_copy(update={'macro_backfill_end': None, 'macro_backfill_request_limit': 12})
    old = seed(factory, ProcessLocalJobLock())
    return settings, factory, engine, old


def test_pg_activation_preserves_old_contract_and_explicit_pair_publication(database, monkeypatch):
    settings, factory, _, old = prepared(database)
    source = CurrentSource()
    report = execute(settings, source)
    assert report.status == 'completed' and report.planned == 1 and report.row_count == 1
    assert report.publication is None and len(source.calls) == 1
    assert read_govbond_history_status(settings)['completedMonths'] == 12
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == old
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert session.get(MacroSeriesState, SID).latest_observed_at == date(2026, 10, 6)
    # 满足宏观质检的其他16项受控core；原missing三项仍无值。
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for definition in catalog.series:
            if definition.id == SID or not definition.sources:
                continue
            period = date(2026, 7, 1) if definition.frequency == 'quarterly' else date(2026, 9, 1) if definition.frequency == 'monthly' else date(2026, 10, 6)
            session.add(MacroObservation(source=definition.sources[0].provider, series_id=definition.id,
                name=definition.name, region=definition.region, frequency=definition.frequency,
                unit=definition.unit, seasonal_adjustment=definition.seasonal_adjustment,
                observed_at=period, released_at=HISTORY_TIME, revision='controlled-pg', value=1,
                source_url=str(definition.sources[0].source_url)))
    from app.cli import main as cli
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli, 'datetime', FixedTime)
    published = _run_macro_rebuild(settings)
    assert set(published.products) == {'macro-insights', 'macro-transmission'}
    with factory() as session:
        snapshots = session.scalars(select(PublishedSnapshot)).all()
        assert len(snapshots) == 2 and len({p.version for p in snapshots}) == 1
        for snapshot in snapshots:
            state = next(s for s in snapshot.payload['seriesStatus'] if s['id'] == SID)
            assert state['actualSource'] == 'chinamoney' and state['observedAt'] == '2026-10-06'
        assert session.scalar(select(JobCursor)).cursor_value == old
    assert execute(settings, CurrentSource()).row_count == 0


def test_pg_shared_lock_stops_before_preflight_or_network(database):
    settings, factory, engine, old = prepared(database)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    source = CurrentSource()
    try:
        assert execute(settings, source).status == 'locked'
    finally:
        lock.release(data_publication_lock_key())
    assert source.calls == []
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == old
        assert session.get(MacroSeriesState, SID).active_source is None
