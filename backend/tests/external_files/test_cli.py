import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from app.cli.main import app
from app.cli import main as main_module
from app.db.base import Base, DerivedBase, ExternalBase, MartBase
from app.db.models import MacroObservation, MacroSeriesState, PublishedSnapshot
from app.macro.jobs.macro_sync import MacroPublishReport
from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.connectors.external_files.reader import ExternalFileReader
from app.ingestion.external_import import ExternalImporter


def _import_source_less(factory: sessionmaker[Session], catalog: MacroSeriesCatalog, directory: Path,
                        observed_at: date, released_at: datetime, revision: str) -> None:
    records = [{
        "schemaVersion": "1.0", "recordType": "macro", "source": "controlled-offline-cli",
        "seriesId": series.id, "name": series.name, "region": series.region,
        "frequency": series.frequency, "unit": series.unit,
        "seasonalAdjustment": series.seasonal_adjustment,
        "observedAt": observed_at.isoformat(), "releasedAt": released_at.isoformat(),
        "revision": revision, "value": "1", "sourceUrl": "https://example.com/controlled-cli",
    } for series in catalog.series if not series.sources]
    path = directory / "controlled-source-less.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    imported = ExternalImporter(factory, ExternalFileReader(directory)).import_file(path)
    assert imported.status == "imported" and imported.imported_count == len(records) and imported.rejected_count == 0


def test_validate_import_does_not_require_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DANG_EXTERNAL_IMPORT_PATH", str(tmp_path))
    path = tmp_path / "valid.jsonl"
    path.write_text(json.dumps({"schemaVersion": "1.0", "recordType": "content", "source": "x", "contentType": "news", "title": "t", "publishedAt": "2026-08-17T00:00:00Z", "sourceUrl": "https://example.com", "securities": [], "copyrightStatus": "link_only"}), encoding="utf-8")
    result = CliRunner().invoke(app, ["validate-import", str(path)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "fileHash": hashlib.sha256(path.read_bytes()).hexdigest(), "rowCount": 1,
        "macroCount": 0, "contentCount": 1, "errors": [],
    }
    path.write_text("{", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate-import", str(path)])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "fileHash": hashlib.sha256(path.read_bytes()).hexdigest(), "rowCount": 0,
        "macroCount": 0, "contentCount": 0,
        "errors": [{"row": 1, "errors": [{"message": "JSON 无效"}]}],
    }


def test_rebuild_external_marts_rejects_unknown_product() -> None:
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "unknown"])
    assert result.exit_code == 2


def test_rebuild_macro_emits_publication_version_products_and_quality(monkeypatch) -> None:
    from app.marts.contracts import QualityResult

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: object())
    monkeypatch.setattr(main_module, "_run_macro_rebuild", lambda settings: MacroPublishReport(
        "completed", "release-1", ("macro-transmission", "macro-insights"),
        (QualityResult("coverage", "blocking", True, {}),),
        ("macro-transmission", "macro-insights"),
    ), raising=False)
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "macro"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["version"] == "release-1"
    assert payload["products"] == ["macro-transmission", "macro-insights"]
    assert payload["quality"][0]["name"] == "coverage"


@pytest.mark.parametrize("status,code,expected", [
    ("completed", None, 0),
    ("no_change", "CONTENT_NO_DOCUMENTS", 0),
    ("blocked", "CONTENT_DATABASE_REQUIRED", 3),
    ("blocked", "CONTENT_LOCK_BUSY", 5),
    ("blocked", "CONTENT_QUALITY_BLOCKED", 4),
    ("failed", "CONTENT_REBUILD_FAILED", 4),
])
def test_rebuild_content_reports_real_runtime_status(monkeypatch, status, code, expected) -> None:
    settings = main_module.Settings(database_url="sqlite://")
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: settings)
    received = []
    def rebuild(actual):
        received.append(actual)
        return {"status": status, "code": code, "requestCount": 0, "copyrightVerified": False}
    monkeypatch.setattr(main_module, "rebuild_content_marts", rebuild, raising=False)
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "content"])
    assert result.exit_code == expected, result.output
    assert received == [settings]
    assert json.loads(result.stdout) == {"status": status, "code": code,
        "requestCount": 0, "copyrightVerified": False}


def test_rebuild_content_configuration_error_does_not_expose_secret(monkeypatch) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: (_ for _ in ()).throw(
        ValueError("postgresql://user:must-not-print@localhost/db")))
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "content"])
    assert result.exit_code == 3
    assert "must-not-print" not in result.output
    assert json.loads(result.stdout)["code"] == "CONTENT_CONFIGURATION_UNAVAILABLE"


def test_rebuild_macro_publication_failure_exits_four(monkeypatch) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: main_module.Settings(database_url="sqlite://"))
    monkeypatch.setattr(main_module, "_run_macro_rebuild", lambda settings: (_ for _ in ()).throw(
        RuntimeError("publication failed")
    ))
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "macro"])
    assert result.exit_code == 4


def test_rebuild_macro_lock_conflict_exits_five(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        execution_options={"schema_translate_map": {
            "raw": None, "core": None, "ops": None, "derived": None, "mart": None,
        }},
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    class BusyLock:
        def try_acquire(self, key):
            assert key == "data-publication"
            return False

    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: (engine, factory))
    monkeypatch.setattr(main_module, "job_lock_for_engine", lambda engine: BusyLock())
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: main_module.Settings(database_url="sqlite://"))
    result = CliRunner().invoke(app, ["rebuild-external-marts", "--product", "macro"])
    assert result.exit_code == 5, result.output
    assert "MacroRebuildLockedError" in result.stderr


@pytest.mark.parametrize("observed_days,released_hours,checked_hours,expected", [
    (1, 1, 2, "fresh"),
    (1, 3, 2, "stale"),
    (1, 3, -1, "fresh"),
    (1000, 1, 2, "stale"),
])
def test_macro_rebuild_recomputes_stale_from_visible_revision_and_state_timing(
    observed_days, released_hours, checked_hours, expected,
) -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.series[0]
    as_of = datetime(2026, 9, 20, 12, tzinfo=UTC)
    observed_at = (as_of - timedelta(days=observed_days)).date()
    row = MacroObservation(
        source=series.sources[0].provider, series_id=series.id,
        name=series.name, region=series.region, frequency=series.frequency,
        unit=series.unit, seasonal_adjustment=series.seasonal_adjustment,
        observed_at=observed_at, released_at=as_of - timedelta(hours=released_hours),
        revision="imported-revision", value=Decimal("1"), source_url="https://example.com",
    )
    state = MacroSeriesStateView(
        series.id, catalog.catalog_version, as_of - timedelta(hours=checked_hours),
        as_of - timedelta(days=2), observed_at, as_of - timedelta(hours=4),
        row.source, False, True, "source_failed", 1,
    )
    outcomes = main_module._macro_rebuild_outcomes(catalog, (row,), {series.id: state}, as_of)
    assert outcomes[0].status == expected
    assert outcomes[0].actual_source == row.source
    assert all(item.status == "missing" for item in outcomes[1:])
    if observed_days == 1 and checked_hours > 0:
        older_state = replace(state, latest_observed_at=observed_at - timedelta(days=1))
        assert main_module._macro_rebuild_outcomes(
            catalog, (row,), {series.id: older_state}, as_of,
        )[0].status == "fresh"


def test_macro_rebuild_publishes_imported_observations_through_macro_job(monkeypatch, tmp_path: Path) -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    now = datetime.now(UTC)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        execution_options={"schema_translate_map": {
            "raw": None, "core": None, "ops": None, "derived": None, "mart": None,
        }},
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(MacroSeriesState(
            series_id=catalog.series[0].id,
            catalog_version=catalog.catalog_version,
            last_checked_at=now - timedelta(days=2),
            last_success_at=now - timedelta(days=3),
            latest_observed_at=(now - timedelta(days=3)).date(),
            latest_released_at=now - timedelta(days=3),
            active_source=catalog.series[0].sources[0].provider,
            fallback_used=True, stale=True, stale_reason="source_failed",
            consecutive_failures=1,
        ))
        session.add_all(MacroObservation(
            source=series.sources[0].provider if series.sources else "external_file", series_id=series.id,
            name=series.name, region=series.region, frequency=series.frequency,
            unit=series.unit, seasonal_adjustment=series.seasonal_adjustment,
            observed_at=(now - timedelta(days=1)).date(),
            released_at=now - timedelta(hours=1),
            revision="initial", value=Decimal("1"), source_url="https://example.com",
        ) for series in catalog.series if series.sources)
    _import_source_less(factory, catalog, tmp_path, (now - timedelta(days=1)).date(), now - timedelta(hours=1), "initial")
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: (engine, factory))
    monkeypatch.setattr(engine, "dispose", lambda: None)
    monkeypatch.setattr(main_module, "BuildAndPublishJob", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("stock rebuild must not run")
    ))
    report = main_module._run_macro_rebuild(main_module.Settings(database_url="sqlite://"))
    assert report.status == "completed", report.quality
    assert report.products == ("macro-transmission", "macro-insights")
    with factory() as session:
        rows = session.scalars(select(PublishedSnapshot)).all()
    assert {row.product for row in rows} == set(report.products)
    assert {row.version for row in rows} == {report.version}
    assert all(all(item["stale"] is False for item in row.payload["seriesStatus"]) for row in rows)
    assert all(row.payload["seriesStatus"][0]["fallbackUsed"] is False for row in rows)
    for row in rows:
        status = row.payload["seriesStatus"][0]
        assert status["staleReason"] is None
        assert status["lastCheckedAt"] == (now - timedelta(days=2)).isoformat()
        assert status["lastSuccessAt"] == (now - timedelta(days=3)).isoformat()


@pytest.mark.parametrize("state_kind,expected_reason", [
    ("absent", "MACRO_VALUE_EXPIRED"),
    ("superseded_failure", "MACRO_VALUE_EXPIRED"),
    ("current_failure", "source_failed"),
])
def test_macro_rebuild_publishes_distinct_expiry_and_provider_failure_reasons(
    monkeypatch, state_kind, expected_reason, tmp_path: Path,
) -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    now = datetime.now(UTC)
    old_date = (now - timedelta(days=1000)).date()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        execution_options={"schema_translate_map": {
            "raw": None, "core": None, "ops": None, "derived": None, "mart": None,
        }},
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        if state_kind != "absent":
            session.add(MacroSeriesState(
                series_id=catalog.series[0].id, catalog_version=catalog.catalog_version,
                last_checked_at=now - timedelta(minutes=30),
                latest_observed_at=old_date - timedelta(days=1) if state_kind == "superseded_failure" else old_date,
                active_source=catalog.series[0].sources[0].provider,
                fallback_used=False, stale=True, stale_reason="source_failed",
                consecutive_failures=1,
            ))
        session.add_all(MacroObservation(
            source=series.sources[0].provider if series.sources else "external_file",
            series_id=series.id, name=series.name, region=series.region,
            frequency=series.frequency, unit=series.unit,
            seasonal_adjustment=series.seasonal_adjustment,
            observed_at=old_date, released_at=now - timedelta(hours=1),
            revision="expired-import", value=Decimal("1"), source_url="https://example.com",
        ) for series in catalog.series if series.sources)
    _import_source_less(factory, catalog, tmp_path, old_date, now - timedelta(hours=1), "expired-import")
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: (engine, factory))
    dispose = engine.dispose
    monkeypatch.setattr(engine, "dispose", lambda: None)
    try:
        report = main_module._run_macro_rebuild(main_module.Settings(database_url="sqlite://"))
        assert report.status == "completed", report.quality
        with factory() as session:
            snapshots = session.scalars(select(PublishedSnapshot)).all()
        assert {row.product for row in snapshots} == {"macro-transmission", "macro-insights"}
        for snapshot in snapshots:
            assert snapshot.stale is True
            status = snapshot.payload["seriesStatus"][0]
            assert status["stale"] is True
            assert status["staleReason"] == expected_reason
            assert status["observedAt"] == old_date.isoformat()
            assert all(item["staleReason"] == "MACRO_VALUE_EXPIRED" for item in snapshot.payload["seriesStatus"][1:])
    finally:
        dispose()


def test_macro_rebuild_uses_one_observation_view_when_import_arrives_during_publish(monkeypatch, tmp_path: Path) -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    now = datetime.now(UTC)
    old_date = (now - timedelta(days=1)).date()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        execution_options={"schema_translate_map": {
            "raw": None, "core": None, "ops": None, "derived": None, "mart": None,
        }},
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(MacroSeriesState(
            series_id=catalog.series[0].id, catalog_version=catalog.catalog_version,
            last_checked_at=now - timedelta(hours=2),
            last_success_at=now - timedelta(hours=2),
            latest_observed_at=old_date, latest_released_at=now - timedelta(hours=1),
            active_source=catalog.series[0].sources[0].provider,
            fallback_used=False, stale=False, consecutive_failures=0,
        ))
        session.add_all(MacroObservation(
            source=series.sources[0].provider if series.sources else "external_file",
            series_id=series.id, name=series.name, region=series.region,
            frequency=series.frequency, unit=series.unit,
            seasonal_adjustment=series.seasonal_adjustment,
            observed_at=old_date, released_at=now - timedelta(hours=1),
            revision="initial", value=Decimal("1"), source_url="https://example.com",
        ) for series in catalog.series if series.sources)
    _import_source_less(factory, catalog, tmp_path, old_date, now - timedelta(hours=1), "initial")
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: (engine, factory))
    monkeypatch.setattr(engine, "dispose", lambda: None)
    held = False
    reads = {"states": 0, "observations": 0}

    class PublicationLock:
        def try_acquire(self, key):
            nonlocal held
            assert key == "data-publication"
            held = True
            return True

        def release(self, key):
            nonlocal held
            assert held and key == "data-publication"
            held = False

    get_states = main_module.MacroSeriesStateRepository.get_all
    get_observations = main_module.MacroRepository.all_observations

    def read_states(repository):
        assert held, "state view must be read under publication lock"
        reads["states"] += 1
        return get_states(repository)

    def read_observations(repository, series_ids, as_of):
        assert held, "observation view must be read under publication lock"
        reads["observations"] += 1
        return get_observations(repository, series_ids, as_of)

    monkeypatch.setattr(main_module, "job_lock_for_engine", lambda engine: PublicationLock())
    monkeypatch.setattr(main_module.MacroSeriesStateRepository, "get_all", read_states)
    monkeypatch.setattr(main_module.MacroRepository, "all_observations", read_observations)
    original_publish = main_module.MacroPublishJob.publish

    def import_then_publish(job, run_id, as_of, catalog, statuses, **kwargs):
        series = catalog.series[0]
        with factory.begin() as session:
            state = session.get(MacroSeriesState, series.id)
            state.last_checked_at = as_of.astimezone(UTC)
            state.stale = True
            state.stale_reason = "source_failed"
            session.add(MacroObservation(
                source=series.sources[0].provider, series_id=series.id,
                name=series.name, region=series.region,
                frequency=series.frequency, unit=series.unit,
                seasonal_adjustment=series.seasonal_adjustment,
                observed_at=as_of.astimezone(UTC).date(),
                released_at=as_of.astimezone(UTC),
                revision="later-import", value=Decimal("2"),
                source_url="https://example.com",
            ))
        return original_publish(job, run_id, as_of, catalog, statuses, **kwargs)

    original_build = main_module.MacroMartBuilder.build

    def import_between_builds(builder, as_of, version, **kwargs):
        candidates = original_build(builder, as_of, version, **kwargs)
        if version == "pending":
            with factory.begin() as session:
                imported = session.scalar(select(MacroObservation).where(
                    MacroObservation.revision == "later-import",
                ))
                imported.value = Decimal("3")
        return candidates

    monkeypatch.setattr(main_module.MacroPublishJob, "publish", import_then_publish)
    monkeypatch.setattr(main_module.MacroMartBuilder, "build", import_between_builds)
    report = main_module._run_macro_rebuild(main_module.Settings(database_url="sqlite://"))
    assert report.status == "completed"
    with factory() as session:
        rows = session.scalars(select(PublishedSnapshot)).all()
    assert len(rows) == 2
    assert all(row.payload["seriesStatus"][0]["observedAt"] == old_date.isoformat() for row in rows)
    assert all(row.payload["seriesStatus"][0]["stale"] is False for row in rows)
    assert all(row.payload["seriesStatus"][0]["lastCheckedAt"] == (now - timedelta(hours=2)).isoformat() for row in rows)
    assert reads == {"states": 1, "observations": 1}
    assert held is False
    assert {row.version for row in rows} == {report.version}
    insights = next(row for row in rows if row.product == "macro-insights")
    transmission = next(row for row in rows if row.product == "macro-transmission")
    assert insights.payload["sandboxIndicators"][0]["values"][0]["value"] == 1
    assert transmission.payload["transmission"]["modes"][0]["mainNodes"][0]["series"][0]["value"] == 1
    degradation = next(item for item in report.quality if item.name == "macro-degradation")
    assert not degradation.passed  # Three traced offline sources are advisory fallback, not stale/missing.
    assert degradation.details["stale"] == [] and degradation.details["missing"] == []
    assert degradation.details["fallback"] == [
        "cn.credit.tsf_stock_yoy",
        "cn.fx.usdcny",
    ]
    with factory() as session:
        assert session.scalar(select(MacroObservation.value).where(
            MacroObservation.revision == "later-import",
        )) == Decimal("3")
