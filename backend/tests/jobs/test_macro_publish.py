from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.models import MacroObservation, MacroSeriesState, PublishedSnapshot, QualityCheckResult
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_sync import MacroSeriesOutcome
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.contracts import MartCandidate
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.marts.publication import SnapshotPublisher


from backend.tests.macro.controlled_catalog import controlled_catalog
CATALOG = controlled_catalog()
AS_OF = datetime(2026, 9, 20, 10, tzinfo=UTC)


class History:
    def __init__(self) -> None:
        self.quality = ()

    def record_quality(self, run_id, quality) -> None:
        assert run_id == "macro-run"
        self.quality = tuple(quality)


def setup(factory, count: int):
    engine = factory.kw["bind"]
    statuses = seed_observations(factory, count)
    history = History()
    publisher = SnapshotPublisher(factory)
    job = MacroPublishJob(MacroMartBuilder(MacroRepository(factory), catalog=CATALOG), publisher, history)
    return engine, factory, job, history, publisher, statuses


def seed_observations(factory, count):
    with factory.begin() as session:
        session.add_all(
            MacroObservation(source=item.sources[0].provider if item.sources else "fixture",
                             series_id=item.id, name=item.name, region=item.region,
                             frequency=item.frequency, unit=item.unit,
                             seasonal_adjustment=item.seasonal_adjustment,
                             observed_at=date(2026, 8, 1), released_at=AS_OF - timedelta(days=1),
                             revision="initial", value=Decimal("1"), source_url="https://example.com")
            for item in CATALOG.series[:count]
        )
    return tuple(MacroSeriesOutcome(item.id, "fresh", item.sources[0].provider if item.sources else "fixture", 0, 0, 0, ()) for item in CATALOG.series)


def test_publishes_only_two_macro_products_at_exact_instant(pg_factory) -> None:
    engine, factory, job, history, _, statuses = setup(pg_factory, 20)
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.status == "completed" and not report.blocked
        assert report.products == ("macro-transmission", "macro-insights")
        assert report.promoted_products == report.products
        assert all(item.passed for item in history.quality if item.severity == "blocking")
        with factory() as session:
            rows = session.scalars(select(PublishedSnapshot)).all()
        assert {row.product for row in rows} == set(report.products)
        assert all(row.as_of == AS_OF for row in rows)
    finally:
        engine.dispose()


def test_fifteen_series_blocks_without_pointer_changes(pg_factory) -> None:
    engine, factory, job, history, _, statuses = setup(pg_factory, 15)
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.status == "blocked" and report.blocked
        assert report.version is None and report.products == ()
        assert not next(item for item in history.quality if item.name == "macro-coverage-at-least-80").passed
        with factory() as session:
            assert session.scalars(select(PublishedSnapshot)).all() == []
    finally:
        engine.dispose()


def test_stale_status_keeps_value_and_state_reason_in_both_products(pg_factory) -> None:
    engine, factory, _, history, publisher, statuses = setup(pg_factory, 20)
    with factory.begin() as session:
        session.add(MacroSeriesState(
            series_id=CATALOG.series[0].id, catalog_version=CATALOG.catalog_version,
            last_checked_at=AS_OF - timedelta(hours=1),
            last_success_at=AS_OF - timedelta(days=2),
            active_source=statuses[0].actual_source,
            fallback_used=False, stale=True, stale_reason="source_failed",
            consecutive_failures=1,
        ))
    builder = MacroMartBuilder(
        MacroRepository(factory), catalog=CATALOG,
        state_repository=MacroSeriesStateRepository(factory, CATALOG.catalog_version),
    )
    job = MacroPublishJob(builder, publisher, history)
    stale = MacroSeriesOutcome(statuses[0].series_id, "stale", statuses[0].actual_source, 0, 0, 0, ())
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, (stale, *statuses[1:]))
        assert report.status == "completed"
        with factory() as session:
            snapshots = session.scalars(select(PublishedSnapshot)).all()
        assert len(snapshots) == 2 and all(item.stale for item in snapshots)
        for snapshot in snapshots:
            first = snapshot.payload["seriesStatus"][0]
            assert first["observedAt"] == "2026-08-01"
            assert first["stale"] is True
            assert first["staleReason"] == "source_failed"
            assert first["lastCheckedAt"] == (AS_OF - timedelta(hours=1)).isoformat()
    finally:
        engine.dispose()


def test_blocked_macro_run_persists_quality_checks(pg_factory) -> None:
    engine, factory, _, _, publisher, statuses = setup(pg_factory, 15)
    history = SqlMacroSyncHistory(factory)
    run_id = history.start("2026-W38:" + CATALOG.catalog_version, AS_OF,
                           CATALOG.catalog_version, CATALOG.catalog_hash, "test", 1)
    job = MacroPublishJob(MacroMartBuilder(MacroRepository(factory), catalog=CATALOG), publisher, history)
    try:
        report = job.publish(run_id, AS_OF, CATALOG, statuses)
        assert report.blocked
        with factory() as session:
            checks = session.scalars(select(QualityCheckResult).where(QualityCheckResult.job_run_id == run_id)).all()
        assert len(checks) == len(report.quality)
        assert any(item.check_name == "macro-coverage-at-least-80" and not item.passed for item in checks)
    finally:
        engine.dispose()


def test_duplicate_raw_observation_blocks_and_records_quality(pg_factory) -> None:
    engine, factory, _, history, publisher, statuses = setup(pg_factory, 20)

    class DuplicateRepository(MacroRepository):
        def all_observations(self, series_ids, as_of):
            rows = super().all_observations(series_ids, as_of)
            return (*rows, rows[0])

    job = MacroPublishJob(MacroMartBuilder(DuplicateRepository(factory), catalog=CATALOG), publisher, history)
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.blocked and report.promoted_products == ()
        check = next(item for item in history.quality if item.name == "macro-observation-identity-unique")
        assert check.severity == "blocking" and not check.passed
        assert publisher.current("macro-insights") is None
    finally:
        engine.dispose()


def test_older_macro_replay_preserves_both_newer_pointers(pg_factory) -> None:
    engine, factory, job, _, publisher, statuses = setup(pg_factory, 20)
    try:
        current = job.publish("macro-run", AS_OF, CATALOG, statuses)
        replay = job.publish("macro-run", AS_OF - timedelta(hours=1), CATALOG, statuses)
        assert replay.status == "completed" and replay.promoted_products == ()
        assert replay.version != current.version
        assert {publisher.current(product).version for product in current.products} == {current.version}
        with factory() as session:
            assert len(session.scalars(select(PublishedSnapshot)).all()) == 2
    finally:
        engine.dispose()


def test_invalid_raw_value_blocks_publication_and_records_quality(pg_factory) -> None:
    engine, factory, _, history, publisher, statuses = setup(pg_factory, 20)

    class InvalidRepository(MacroRepository):
        def all_observations(self, series_ids, as_of):
            rows = super().all_observations(series_ids, as_of)
            rows[0].value = Decimal("NaN")
            return rows

    job = MacroPublishJob(MacroMartBuilder(InvalidRepository(factory), catalog=CATALOG), publisher, history)
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.blocked
        assert not next(item for item in history.quality if item.name == "macro-values-valid").passed
        assert publisher.current("macro-transmission") is None
    finally:
        engine.dispose()


def test_later_released_invalid_revision_does_not_block_earlier_replay(pg_factory) -> None:
    engine, factory, job, history, publisher, statuses = setup(pg_factory, 20)
    definition = CATALOG.series[0]
    with factory.begin() as session:
        session.add(MacroObservation(
            source=definition.sources[0].provider if definition.sources else "fixture",
            series_id=definition.id, name=definition.name, region=definition.region,
            frequency=definition.frequency, unit="invalid-unit",
            seasonal_adjustment=definition.seasonal_adjustment,
            observed_at=date(2026, 8, 1), released_at=AS_OF + timedelta(hours=1),
            revision="future-invalid", value=Decimal("1"), source_url="https://example.com",
        ))
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.status == "completed"
        assert next(item for item in history.quality if item.name == "macro-values-valid").passed
        assert publisher.current("macro-insights") is not None
    finally:
        engine.dispose()


def test_mixed_current_macro_times_report_pair_skip(pg_factory) -> None:
    engine, _, job, history, publisher, statuses = setup(pg_factory, 20)
    publisher.publish_many((MartCandidate("macro-transmission", AS_OF + timedelta(hours=1), {}),), "transmission-new", ())
    publisher.publish_many((MartCandidate("macro-insights", AS_OF - timedelta(hours=1), {}),), "insights-old", ())
    try:
        report = job.publish("macro-run", AS_OF, CATALOG, statuses)
        assert report.status == "completed"
        assert report.promoted_products == ()
        assert publisher.current("macro-transmission").version == "transmission-new"
        assert publisher.current("macro-insights").version == "insights-old"
        assert all(item.passed for item in history.quality if item.severity == "blocking")
    finally:
        engine.dispose()


def test_unchanged_routine_rebuild_reuses_pair_after_quality_validation(pg_factory):
    from sqlalchemy import func
    from app.db.models import PageSnapshot
    engine, factory, job, history, publisher, statuses = setup(pg_factory, 20)
    try:
        first = job.publish('macro-run', AS_OF, CATALOG, statuses)
        history.quality = ()
        second = job.publish('macro-run', AS_OF+timedelta(seconds=1), CATALOG, statuses, reuse_unchanged=True)
        assert second.version == first.version and second.promoted_products == ()
        assert history.quality and all(q.passed for q in history.quality if q.severity == 'blocking')
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PageSnapshot)) == 2
        assert {publisher.current(p).as_of for p in first.products} == {AS_OF}
    finally:
        engine.dispose()


def test_routine_rebuild_with_changed_value_still_promotes_after_skipped_collection(pg_factory):
    engine, factory, job, history, publisher, statuses = setup(pg_factory, 20)
    try:
        first = job.publish('macro-run', AS_OF, CATALOG, statuses)
        with factory.begin() as session:
            row = session.scalar(select(MacroObservation).where(MacroObservation.series_id == CATALOG.series[0].id))
            row.value = Decimal('2')
        second = job.publish('macro-run', AS_OF+timedelta(seconds=1), CATALOG, statuses, reuse_unchanged=True)
        assert second.version != first.version and len(second.promoted_products) == 2
    finally:
        engine.dispose()
