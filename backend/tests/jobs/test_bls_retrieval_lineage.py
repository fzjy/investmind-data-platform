from datetime import UTC, date, datetime, timedelta

from app.connectors.macro.bls import BlsMacroProvider
from app.connectors.macro.contracts import MacroFetchWindow
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.db.models import SourcePayload
from sqlalchemy import select
from backend.tests.jobs.test_build_and_publish_integration import _factory
from backend.tests.connectors.macro.test_us_official import FakeBlsTransport


def test_selected_bls_retrieval_date_comes_from_matching_audited_response_not_publication_or_clock() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.require("us.cpi.yoy")
    source = next(source for source in series.sources if source.provider == "bls")
    captured = datetime(2026, 9, 20, 8, tzinfo=UTC)
    window = MacroFetchWindow(date(2026, 8, 1), date(2026, 9, 20), captured)
    payload = {"status": "REQUEST_SUCCEEDED", "Results": [{"series": [{"seriesID": source.series, "data": [
        {"year": "2025", "period": "M08", "value": "100"},
        {"year": "2026", "period": "M08", "value": "103"},
    ]}]}]}
    fetched = BlsMacroProvider(FakeBlsTransport(payload)).fetch(series, source, window)
    assert fetched.status == "success"
    factory = _factory()
    repository = MacroObservationRepository(factory)
    builder = MacroMartBuilder(MacroRepository(factory), catalog=catalog)
    try:
        batch = repository.audit_request(series, source, window, fetched, captured, "first")
        repository.persist(series, fetched, captured, batch_id=batch)
        later = captured + timedelta(hours=1)
        second = repository.audit_request(series, source, window, fetched, later, "second")
        assert repository.persist(series, fetched, later, batch_id=second).unchanged == 1
        wrong_payload = {"status": "REQUEST_SUCCEEDED", "Results": [{"series": [{"seriesID": source.series, "data": [
            {"year": "2025", "period": "M08", "value": "100"},
            {"year": "2026", "period": "M08", "value": "105"},
        ]}]}]}
        wrong = BlsMacroProvider(FakeBlsTransport(wrong_payload)).fetch(series, source, window)
        repository.audit_request(series, source, window, wrong, later + timedelta(hours=1), "unselected-value")
        for cutoff, want in ((captured, captured), (later, later), (later + timedelta(hours=1), later)):
            candidate = builder.build(cutoff, "controlled", series_ids=(series.id,))[1]
            status = candidate.payload["seriesStatus"][0]
            assert status["actualSource"] == "bls"
            assert status["releasedAt"] == captured.isoformat()  # Capture semantics, not a historical release claim.
            assert status.get("retrievedAt") == want.isoformat()
        # Older successful audits without normalized lineage cannot invent a retrieval date.
        with factory.begin() as session:
            for audit in session.scalars(select(SourcePayload)):
                value = dict(audit.payload)
                value["data"] = {key: item for key, item in value["data"].items() if key != "normalizedObservations"}
                audit.payload = value
        status = builder.build(later, "legacy", series_ids=(series.id,))[1].payload["seriesStatus"][0]
        assert status["retrievedAt"] is None and status["releasedAt"] == captured.isoformat()
    finally:
        factory.kw["bind"].dispose()


def test_bls_retrieval_lineage_matches_numeric_storage_precision_for_local_pc1() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.require("us.cpi.yoy")
    source = next(source for source in series.sources if source.provider == "bls")
    captured = datetime(2026, 9, 20, 8, tzinfo=UTC)
    window = MacroFetchWindow(date(2026, 8, 1), date(2026, 9, 20), captured)
    payload = {"status": "REQUEST_SUCCEEDED", "Results": [{"series": [{"seriesID": source.series, "data": [
        {"year": "2025", "period": "M08", "value": "310.394"},
        {"year": "2026", "period": "M08", "value": "319.679"},
    ]}]}]}
    fetched = BlsMacroProvider(FakeBlsTransport(payload)).fetch(series, source, window)
    factory = _factory()
    try:
        repository = MacroObservationRepository(factory)
        batch = repository.audit_request(series, source, window, fetched, captured, "decimal")
        repository.persist(series, fetched, captured, batch_id=batch)
        status = MacroMartBuilder(MacroRepository(factory), catalog=catalog).build(
            captured, "controlled", series_ids=(series.id,))[1].payload["seriesStatus"][0]
        assert status["retrievedAt"] == captured.isoformat()
    finally:
        factory.kw["bind"].dispose()
