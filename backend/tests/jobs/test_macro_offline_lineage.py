from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.external_files.reader import ExternalFileReader
from app.db.models import MacroObservation
from app.ingestion.external_import import ExternalImporter
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from backend.tests.jobs.test_build_and_publish_integration import _real_build_and_publish_job, _seed_complete_core_sample
from backend.tests.jobs.test_macro_publish import History

CATALOG = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
AS_OF = datetime(2026, 8, 24, tzinfo=UTC)


def import_controlled_macro(factory: sessionmaker[Session], directory: Path, count: int = 20, *, value: str = "1.25") -> None:
    path = directory / "controlled-macro.jsonl"
    records = [{
        "schemaVersion": "1.0", "recordType": "macro", "source": "controlled-offline",
        "seriesId": item.id, "name": item.name, "region": item.region,
        "frequency": item.frequency, "unit": item.unit,
        "seasonalAdjustment": item.seasonal_adjustment,
        "observedAt": "2026-08-23" if item.frequency == "daily" else "2026-08-01", "releasedAt": "2026-08-23T00:00:00Z",
        "revision": "controlled-initial", "value": value,
        "sourceUrl": f"https://example.com/controlled/{item.id}",
    } for item in CATALOG.series[:count]]
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    assert ExternalImporter(factory, ExternalFileReader(directory)).import_file(path).status == "imported"


@pytest.mark.parametrize("value, stored", [
    ("1.1234567890123", "1.1234567890"),
    ("-1.1234567890123", "-1.1234567890"),
    ("1.12345678909", "1.1234567891"),
])
def test_real_import_rounding_preserves_lineage_and_publication(pg_factory, tmp_path: Path, value: str, stored: str) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path, value=value)
    with factory() as session:
        assert session.scalar(select(MacroObservation.value)) == Decimal(stored)
    rows = MacroRepository(factory).visible_observations(tuple(item.id for item in CATALOG.series), AS_OF)
    assert len(rows) == 20
    result = MacroPublishJob(MacroMartBuilder(MacroRepository(factory)), SnapshotPublisher(factory), History()).publish(
        "macro-run", AS_OF, CATALOG, (),
    )
    assert result.status == "completed", result.quality


@pytest.mark.parametrize("changes", [
    {"value": Decimal("1.1234567891")},
    {"source_url": "https://example.com/tampered"},
    {"source": "tampered-offline"},
    {"unit": "tampered-unit"},
])
def test_rounded_import_still_rejects_value_and_metadata_tampering(pg_factory, tmp_path: Path, changes: dict[str, object]) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path, value="1.1234567890123")
    item = CATALOG.series[0]
    with factory.begin() as session:
        row = session.scalar(select(MacroObservation).where(MacroObservation.series_id == item.id))
        for name, value in changes.items():
            setattr(row, name, value)
    assert MacroRepository(factory).visible_observations((item.id,), AS_OF) == ()


@pytest.mark.postgres
def test_postgres_real_import_rounding_matches_stored_precision_without_trusting_tampering(pg_factory, tmp_path: Path) -> None:
    factory = pg_factory
    for index, (value, stored) in enumerate([
        ("1.1234567890123", "1.1234567890"),
        ("1.12345678905", "1.1234567891"),
        ("-1.12345678905", "-1.1234567891"),
        ("123456789012345678.1234567890123", "123456789012345678.1234567890"),
    ]):
        directory = tmp_path / str(index)
        directory.mkdir()
        import_controlled_macro(factory, directory, value=value)
        with factory() as session:
            assert session.scalar(select(MacroObservation.value)) == Decimal(stored)
        assert len(MacroRepository(factory).visible_observations(tuple(item.id for item in CATALOG.series), AS_OF)) == 20
        item = CATALOG.series[0]
        with factory.begin() as session:
            row = session.scalar(select(MacroObservation).where(MacroObservation.series_id == item.id))
            row.value = Decimal(stored) + Decimal("0.0000000001")
        assert MacroRepository(factory).visible_observations((item.id,), AS_OF) == ()


def test_real_import_entry_publishes_all_twenty_with_original_sources(pg_factory, tmp_path: Path) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path)
    publisher = SnapshotPublisher(factory)
    job = MacroPublishJob(MacroMartBuilder(MacroRepository(factory)), publisher, History())
    result = job.publish("macro-run", AS_OF, CATALOG, ())
    assert result.status == "completed", result.quality
    assert result.products == ("macro-transmission", "macro-insights")
    for product in result.products:
        payload = publisher.current(product).payload
        assert len(payload["seriesStatus"]) == 20
        assert {item["actualSource"] for item in payload["seriesStatus"]} == {"controlled-offline"}
        assert all(item["sourceUrl"].startswith("https://example.com/controlled/") for item in payload["seriesStatus"])
        assert all(item["fallbackUsed"] for item in payload["seriesStatus"])
    _seed_complete_core_sample(factory)
    full = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(AS_OF.date(), ("600519.SH",)))
    assert full.status == "completed" and set(result.products) <= set(full.products)


def test_sync_baseline_accepts_traced_offline_for_disabled_online_series(pg_factory, tmp_path: Path) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path)
    item = next(item for item in CATALOG.series if not item.sources)
    latest = MacroObservationRepository(factory).latest_visible(item, AS_OF, AS_OF.date())
    assert latest == ("controlled-offline", date(2026, 8, 1), datetime(2026, 8, 23, tzinfo=UTC))


def test_sync_uses_verified_offline_without_provider_or_faked_identity(pg_factory, tmp_path: Path) -> None:
    from app.connectors.macro.registry import MacroProviderRegistry
    from app.jobs.locks import PostgresAdvisoryJobLock
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
    from app.macro.jobs.macro_sync import MacroSyncJob
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    factory = pg_factory
    import_controlled_macro(factory, tmp_path)
    history = SqlMacroSyncHistory(factory)
    publisher = SnapshotPublisher(factory)
    job = MacroSyncJob(catalog=CATALOG, registry=MacroProviderRegistry(()),
        observation_repository=MacroObservationRepository(factory),
        state_repository=MacroSeriesStateRepository(factory, CATALOG.catalog_version),
        history=history, lock=PostgresAdvisoryJobLock(factory.kw["bind"]),
        publisher=MacroPublishJob(MacroMartBuilder(MacroRepository(factory)), publisher, history),
        clock=lambda: AS_OF, timezone="Asia/Shanghai")
    result = job.run(MacroSyncOptions(AS_OF, 10, CATALOG.catalog_version, CATALOG.catalog_hash, mode="live"))
    assert result.status == "completed", result
    assert result.request_count == 0
    assert all(item.status == "fallback" and item.actual_source == "controlled-offline" and item.fallback_used for item in result.outcomes)


def test_registered_online_source_wins_same_period_but_newer_offline_period_is_kept(pg_factory, tmp_path: Path) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path)
    item = CATALOG.require("cn.cpi.yoy")
    with factory.begin() as session:
        session.add(MacroObservation(source="tushare", series_id=item.id, name=item.name,
            region=item.region, frequency=item.frequency, unit=item.unit, seasonal_adjustment=item.seasonal_adjustment,
            observed_at=date(2026, 8, 1), released_at=datetime(2026, 8, 22, tzinfo=UTC), revision="online",
            value=Decimal("5"), source_url="https://example.com/online"))
    rows = MacroRepository(factory).visible_observations((item.id,), AS_OF)
    assert [(row.source, row.value) for row in rows] == [("tushare", Decimal("5"))]
    with factory.begin() as session:
        from sqlalchemy import select
        online = session.scalar(select(MacroObservation).where(MacroObservation.source == "tushare"))
        online.observed_at = date(2026, 7, 1)
    rows = MacroRepository(factory).visible_observations((item.id,), AS_OF)
    assert [(row.observed_at, row.source) for row in rows] == [(date(2026, 7, 1), "tushare"), (date(2026, 8, 1), "controlled-offline")]
    assert MacroObservationRepository(factory).latest_visible(item, AS_OF, AS_OF.date())[0] == "controlled-offline"


def test_untraced_or_modified_import_is_not_trusted_and_unselected_history_does_not_block(pg_factory, tmp_path: Path) -> None:
    factory = pg_factory
    import_controlled_macro(factory, tmp_path)
    item = CATALOG.series[0]
    with factory.begin() as session:
        session.add(MacroObservation(source="untraced", series_id=item.id, name=item.name,
            region=item.region, frequency=item.frequency, unit="wrong", seasonal_adjustment=item.seasonal_adjustment,
            observed_at=date(2026, 8, 2), released_at=datetime(2026, 8, 23, tzinfo=UTC), revision="old-unknown",
            value=Decimal("2"), source_url="https://example.com/untraced"))
    publisher = SnapshotPublisher(factory)
    job = MacroPublishJob(MacroMartBuilder(MacroRepository(factory)), publisher, History())
    assert job.publish("macro-run", AS_OF, CATALOG, ()).status == "completed"
    with factory.begin() as session:
        from sqlalchemy import select
        imported = session.scalar(select(MacroObservation).where(MacroObservation.series_id == item.id, MacroObservation.source == "controlled-offline"))
        imported.value = Decimal("9")
    rows = MacroRepository(factory).visible_observations((item.id,), AS_OF, {item.id: tuple(source.provider for source in item.sources)})
    assert rows == ()
