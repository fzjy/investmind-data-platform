"""一次性PG17：独立利率同步、core后中断续跑、跨连接锁和显式发布。"""
from datetime import date, datetime, timedelta
import os

from pydantic import SecretStr
import pytest
from sqlalchemy import select

from app.cli import main as cli
from app.db.models import MacroObservation, PublishedSnapshot
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_us_rates_sync import IDS, NOW, ResponseSource, execute, module, preserved, seed


pytestmark = [pytest.mark.postgres,
    pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1',
                      reason='requires disposable PostgreSQL17')]


def prepared(database):
    settings, factory, engine = database
    seed(factory)
    return settings.model_copy(update={'fred_api_key': SecretStr('controlled-fred-key')}), factory, engine


def test_pg_success_repeat_and_explicit_two_product_publication(database, monkeypatch):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    report = execute(settings, ResponseSource())
    assert report.status == 'completed' and report.row_count == 2 and report.publication is None
    assert preserved(factory) == before
    assert execute(settings, ResponseSource()).row_count == 0
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with factory.begin() as session:
        for series in catalog.series:
            if series.id in IDS or not series.sources: continue
            period = date(2026, 7, 1) if series.frequency == 'quarterly' else (
                date(2026, 9, 1) if series.frequency == 'monthly' else date(2026, 10, 9))
            session.add(MacroObservation(source=series.sources[0].provider, series_id=series.id,
                name=series.name, region=series.region, frequency=series.frequency, unit=series.unit,
                seasonal_adjustment=series.seasonal_adjustment, observed_at=period,
                released_at=NOW - timedelta(hours=1), revision='controlled-pg', value=1,
                source_url=str(series.sources[0].source_url)))
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(cli, 'datetime', FixedTime)
    published = cli._run_macro_rebuild(settings)
    assert not published.blocked and set(published.products) == {'macro-insights', 'macro-transmission'}
    with factory() as session:
        pointers = session.scalars(select(PublishedSnapshot)).all()
        assert len(pointers) == 2 and len({r.version for r in pointers}) == 1
        assert all(r.version != 'old-published' for r in pointers)
        for r in pointers:
            target = [s for s in r.payload['seriesStatus'] if s['id'] in IDS]
            assert all(s['actualSource'] == 'fred' and s['observedAt'] == '2026-10-09'
                       and not s['stale'] for s in target)


def test_pg_core_commit_interruption_recovers_in_new_runtime(database, monkeypatch):
    settings, factory, _ = prepared(database)
    before = preserved(factory)
    source = ResponseSource()
    import httpx
    with module().open_us_rates_sync_runtime(settings, transport=httpx.MockTransport(source.handle)) as runtime:
        runtime.job._clock = lambda: NOW
        def interrupted(*args, **kwargs): raise KeyboardInterrupt('controlled core-before-state interruption')
        monkeypatch.setattr(runtime.job._states, 'mark_success', interrupted)
        with pytest.raises(KeyboardInterrupt):
            runtime.job.run(MacroSyncOptions(NOW, settings.macro_history_years,
                runtime.catalog.catalog_version, runtime.catalog.catalog_hash,
                force=True, mode='live', series_ids=IDS, publish=False))
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 3
    recovered = execute(settings, ResponseSource())
    assert recovered.status == 'completed' and recovered.row_count == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 4
    assert preserved(factory) == before


def test_pg_shared_lock_prevents_requests_and_state_mutation(database):
    settings, factory, engine = prepared(database)
    before = preserved(factory)
    lock = PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire(data_publication_lock_key())
    source = ResponseSource()
    try:
        assert execute(settings, source).status == 'locked'
    finally:
        lock.release(data_publication_lock_key())
    assert not source.requests and preserved(factory) == before
