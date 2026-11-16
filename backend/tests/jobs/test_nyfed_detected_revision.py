from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.nyfed import NyFedMacroProvider
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroRepository
from backend.tests.connectors.macro.test_us_official import FakeJsonTransport



def test_nyfed_official_percent_field_and_inclusive_window() -> None:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("us.rate.effective_fed_funds")
    source = next(source for source in series.sources if source.provider == "nyfed").model_copy(update={"field": "percent"})
    captured = datetime(2026, 9, 20, 8, tzinfo=UTC)
    window = MacroFetchWindow(date(2026, 9, 18), date(2026, 9, 18), captured)
    transport = FakeJsonTransport({"refRates": [{"effectiveDate": "2026-09-18", "type": "EFFR", "percent": 4.33, "revisionIndicator": "Y"}]})
    result = NyFedMacroProvider(transport).fetch(series, source, window)
    assert result.status == "success" and result.observations[0].value == Decimal("4.33")
    assert result.observations[0].revision is None and result.observations[0].released_at is None
    assert transport.calls[0][1] == {"startDate": "2026-09-18", "endDate": "2026-09-18", "type": "rate"}
    assert result.raw_payload["payload"]["refRates"][0]["revisionIndicator"] == "Y"


def test_nyfed_repeated_y_is_idempotent_and_changed_value_is_detected_with_strict_replay(pg_factory) -> None:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("us.rate.effective_fed_funds")
    source = next(source for source in series.sources if source.provider == "nyfed")
    captured = datetime(2026, 9, 20, 8, tzinfo=UTC)
    window = MacroFetchWindow(date(2026, 9, 18), date(2026, 9, 18), captured)
    factory = pg_factory
    repository = MacroObservationRepository(factory)
    try:
        for index, value in enumerate(("4.33", "4.33", "4.35")):
            payload = {"refRates": [{"effectiveDate": "2026-09-18", "type": "EFFR", source.field: value, "revisionIndicator": "Y"}]}
            fetched = NyFedMacroProvider(FakeJsonTransport(payload)).fetch(series, source, window)
            assert fetched.status == "success"
            result = repository.persist(series, fetched, captured + timedelta(hours=index))
            assert result.rejected == 0
            assert (result.inserted, result.unchanged) == ((0, 1) if index == 1 else (1, 0))
        visible = MacroRepository(factory)
        for hours, value in ((0, "4.33"), (1, "4.33"), (2, "4.35")):
            rows = visible.visible_observations((series.id,), captured + timedelta(hours=hours))
            assert len(rows) == 1 and rows[0].value == Decimal(value)
            assert rows[0].released_at.replace(tzinfo=UTC) == captured + timedelta(hours=0 if hours < 2 else 2)
    finally:
        factory.kw["bind"].dispose()
