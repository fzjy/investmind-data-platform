from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from app.cli.main import _macro_rebuild_outcomes
from app.config import Settings
from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderObservation, ProviderTransientError
from app.connectors.macro.fred import FredMacroProvider
from app.connectors.macro.transport import MacroHttpTransport
from app.db.models import MacroObservation
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.jobs.sync_status import MacroStatusReader
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository


AS_OF = datetime(2026, 9, 20, tzinfo=UTC)
BEFORE = datetime(2026, 9, 16, tzinfo=UTC)
CLOSED = datetime(2026, 9, 17, tzinfo=UTC)
PERIOD = date(2026, 8, 1)
CATALOG = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
SERIES = CATALOG.require("us.cpi.yoy")


@pytest.fixture
def storage(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    yield factory, MacroObservationRepository(factory), MacroRepository(factory)
    engine.dispose()


def fetch_levels(*, as_of=AS_OF, recover=False):
    rows = [
        {"date": "2025-08-01", "value": "100", "realtime_start": "2026-09-15", "realtime_end": "2026-09-16"},
        {"date": "2025-08-01", "value": ".", "realtime_start": "2026-09-17", "realtime_end": "2026-09-18" if recover else "9999-12-31"},
        {"date": "2026-08-01", "value": "150", "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"},
    ]
    if recover:
        rows.append({"date": "2025-08-01", "value": "125", "realtime_start": "2026-09-19", "realtime_end": "9999-12-31"})

    def handle(request):
        if request.url.path.endswith("vintagedates"):
            dates = [day for day in ("2026-09-15", "2026-09-17", "2026-09-19") if day <= as_of.date().isoformat()]
            return httpx.Response(200, json={"count": len(dates), "offset": 0, "vintage_dates": dates})
        # FRED clips active intervals to the request's real-time boundary.
        visible = [{**row, "realtime_end": min(row["realtime_end"], as_of.date().isoformat())}
                   for row in rows if row["realtime_start"] <= as_of.date().isoformat()]
        return httpx.Response(200, json={"units": "lin", "output_type": 1,
            "count": len(visible), "offset": 0, "observations": visible})

    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        return FredMacroProvider("controlled-key", transport=transport).fetch(
            SERIES, SERIES.sources[0], MacroFetchWindow(PERIOD, as_of.date(), as_of))


def test_missing_dependency_preserves_before_but_excludes_at_cutoff(storage):
    _, repository, reader = storage
    fetched = fetch_levels()
    assert fetched.status == "success"
    repository.persist(SERIES, fetched, AS_OF)
    assert len(reader.visible_observations([SERIES.id], BEFORE)) == 1
    assert repository.latest_visible(SERIES, CLOSED, AS_OF.date()) is None
    assert reader.visible_observations([SERIES.id], CLOSED) == ()
    assert reader.visible_observations([SERIES.id], AS_OF) == ()


def test_replay_can_close_open_validity_but_old_response_cannot_reopen_it(storage):
    factory, repository, reader = storage
    first = fetch_levels(as_of=BEFORE)
    repository.persist(SERIES, first, BEFORE)
    assert reader.visible_observations([SERIES.id], BEFORE)[0].value == 50
    later = repository.persist(SERIES, fetch_levels(), AS_OF)
    assert (later.inserted, later.unchanged, later.rejected) == (0, 1, 0)
    repository.persist(SERIES, first, AS_OF)
    assert reader.visible_observations([SERIES.id], AS_OF) == ()
    with factory() as session:
        row = session.scalar(select(MacroObservation))
        assert getattr(row, "valid_until", None) == CLOSED
    assert len(reader.visible_observations([SERIES.id], BEFORE)) == 1


def test_validity_gap_and_restoration_do_not_leak_future_revision(storage):
    _, repository, reader = storage
    fetched = fetch_levels(recover=True)
    repository.persist(SERIES, fetched, AS_OF)
    assert reader.visible_observations([SERIES.id], datetime(2026, 9, 18, tzinfo=UTC)) == ()
    assert reader.visible_observations([SERIES.id], AS_OF)[0].value == 20


def test_missing_dependency_is_missing_in_rebuild_and_both_products(storage):
    _, repository, reader = storage
    repository.persist(SERIES, fetch_levels(), AS_OF)
    rows = reader.all_observations((SERIES.id,), AS_OF)
    outcomes = _macro_rebuild_outcomes(CATALOG, rows, {}, AS_OF)
    assert next(item for item in outcomes if item.series_id == SERIES.id).status == "missing"
    for candidate in MacroMartBuilder(reader, catalog=CATALOG).build(AS_OF, "validity-v1", observations=rows, states=outcomes):
        status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == SERIES.id)
        assert status["stale"] is True and status["observedAt"] is None


def test_readonly_status_does_not_trust_old_fresh_state_after_dependency_invalidated(storage):
    factory, repository, _ = storage
    repository.persist(SERIES, fetch_levels(), AS_OF)
    states = MacroSeriesStateRepository(factory, CATALOG.catalog_version)
    states.mark_success(SERIES.id, checked_at=BEFORE, active_source="fred",
        latest_observed_at=PERIOD, latest_released_at=datetime(2026, 9, 15, tzinfo=UTC), fallback_used=False)
    counts = MacroStatusReader(factory, Settings(), catalog=CATALOG).read(AS_OF)["series"]
    assert counts["fresh"] == 0
    assert counts["stale"] + counts["missing"] == 20


@pytest.mark.parametrize("provider_status", ["success", "no_change", "failed"])
def test_sync_does_not_recover_invalidated_value_from_prior_state(storage, provider_status):
    from app.connectors.macro.registry import MacroProviderRegistry
    from app.jobs.locks import PostgresAdvisoryJobLock
    from app.macro.jobs.macro_sync import MacroSyncJob
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    from backend.tests.jobs.test_macro_sync import FakeProvider, FakePublisher

    factory, repository, reader = storage
    # Persist closure but retain the last successful state, as happens across runs.
    repository.persist(SERIES, fetch_levels(), AS_OF)
    states = MacroSeriesStateRepository(factory, CATALOG.catalog_version)
    states.mark_success(SERIES.id, checked_at=BEFORE, active_source="fred",
        latest_observed_at=PERIOD, latest_released_at=datetime(2026, 9, 15, tzinfo=UTC), fallback_used=False)
    fetched = fetch_levels() if provider_status == "success" else ProviderFetchResult(
        provider="fred", status=provider_status, observations=(), raw_payload={}, request_count=1,
        error=ProviderTransientError("CONTROLLED_FAILURE") if provider_status == "failed" else None)
    catalog = CATALOG.model_copy(update={"series": (SERIES.model_copy(update={"sources": SERIES.sources[:1]}),)})
    job = MacroSyncJob(catalog=catalog, registry=MacroProviderRegistry((FakeProvider("fred", fetched),)),
        observation_repository=repository, state_repository=states, history=SqlMacroSyncHistory(factory),
        lock=PostgresAdvisoryJobLock(factory.kw["bind"]), publisher=FakePublisher(), timezone="Asia/Shanghai", clock=lambda: AS_OF)
    report = job.run(MacroSyncOptions(AS_OF, 10, catalog.catalog_version, catalog.catalog_hash, mode="live"))
    assert report.outcomes[0].status in {"stale", "missing"}
    current = states.get_all()[0]
    if provider_status == 'no_change':
        # 实际检查成功不代表失效观测恢复；业务缺失仍由可见值独立判断。
        assert current.last_checked_at == current.last_success_at == AS_OF
        assert current.last_error_code is None
    else:
        assert current.stale is True
    assert reader.visible_observations([SERIES.id], AS_OF) == ()
    from app.macro.marts.macro import series_status_payload
    assert series_status_payload(SERIES, current, None, AS_OF)['stale'] is True
    assert report.outcomes[0].recovered_sources == ()


def test_invalidated_primary_allows_a_valid_fallback(storage):
    from decimal import Decimal
    from app.connectors.macro.registry import MacroProviderRegistry
    from app.jobs.locks import PostgresAdvisoryJobLock
    from app.macro.jobs.macro_sync import MacroSyncJob
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    from backend.tests.jobs.test_macro_sync import FakeProvider, FakePublisher

    factory, repository, reader = storage
    fallback = FakeProvider("bls", ProviderFetchResult(provider="bls", status="success",
        observations=(ProviderObservation(provider="bls", provider_series_id="CUUR0000SA0", observed_at=PERIOD,
            released_at=datetime(2026, 9, 18, tzinfo=UTC), revision="controlled-v1", value=Decimal("4"),
            source_url="https://example.test/bls"),), raw_payload={}, request_count=1))
    catalog = CATALOG.model_copy(update={"series": (SERIES,)})
    states = MacroSeriesStateRepository(factory, CATALOG.catalog_version)
    job = MacroSyncJob(catalog=catalog, registry=MacroProviderRegistry((FakeProvider("fred", fetch_levels()), fallback)),
        observation_repository=repository, state_repository=states, history=SqlMacroSyncHistory(factory),
        lock=PostgresAdvisoryJobLock(factory.kw["bind"]), publisher=FakePublisher(), timezone="Asia/Shanghai", clock=lambda: AS_OF)
    report = job.run(MacroSyncOptions(AS_OF, 10, catalog.catalog_version, catalog.catalog_hash, mode="live"))
    assert report.outcomes[0].status == "fallback" and report.outcomes[0].actual_source == "bls"
    assert reader.visible_observations([SERIES.id], AS_OF)[0].source == "bls"


def test_expired_newest_revision_does_not_resurrect_older_open_revision(storage):
    from decimal import Decimal
    factory, _, reader = storage
    with factory.begin() as session:
        for revision, released, until, value in (
            ("old-open", datetime(2026, 9, 15, tzinfo=UTC), None, "50"),
            ("new-closed", BEFORE, CLOSED, "40"),
        ):
            session.add(MacroObservation(source="fred", series_id=SERIES.id, name=SERIES.name,
                region=SERIES.region, frequency=SERIES.frequency, unit=SERIES.unit,
                seasonal_adjustment=SERIES.seasonal_adjustment, observed_at=PERIOD, released_at=released,
                revision=revision, valid_until=until, value=Decimal(value), source_url="https://example.test/fred"))
    assert reader.visible_observations([SERIES.id], AS_OF) == ()


def test_existing_valid_fallback_does_not_resolve_invalid_primary_failure(storage):
    from datetime import timedelta
    from decimal import Decimal
    from app.db.models import JobFailure
    from app.connectors.macro.registry import MacroProviderRegistry
    from app.jobs.locks import PostgresAdvisoryJobLock
    from app.macro.jobs.macro_sync import MacroSyncJob
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    from backend.tests.jobs.test_macro_sync import FakeProvider, FakePublisher

    factory, repository, reader = storage
    repository.persist(SERIES, fetch_levels(as_of=BEFORE), BEFORE)
    fallback_row = ProviderObservation(provider="bls", provider_series_id="CUUR0000SA0", observed_at=PERIOD,
        released_at=datetime(2026, 9, 18, tzinfo=UTC), revision="controlled-v1", value=Decimal("4"),
        source_url="https://example.test/bls")
    repository.persist(SERIES, ProviderFetchResult(provider="bls", status="success", observations=(fallback_row,),
        raw_payload={}, request_count=1), AS_OF)
    catalog = CATALOG.model_copy(update={"series": (SERIES,)})
    states = MacroSeriesStateRepository(factory, catalog.catalog_version)
    states.mark_success(SERIES.id, checked_at=BEFORE, active_source="fred", latest_observed_at=PERIOD,
        latest_released_at=datetime(2026, 9, 15, tzinfo=UTC), fallback_used=False)
    history = SqlMacroSyncHistory(factory)
    prior_run = history.start("prior-validity", AS_OF - timedelta(days=7), catalog.catalog_version, catalog.catalog_hash, "test", 1)
    history.record_failure(prior_run, SERIES.id, "fred", "TIMEOUT", "TIMEOUT", True)
    fallback = FakeProvider("bls", AssertionError("must not refetch valid stored fallback"))
    job = MacroSyncJob(catalog=catalog, registry=MacroProviderRegistry((FakeProvider("fred", fetch_levels()), fallback)),
        observation_repository=repository, state_repository=states, history=history, lock=PostgresAdvisoryJobLock(factory.kw["bind"]),
        publisher=FakePublisher(), timezone="Asia/Shanghai", clock=lambda: AS_OF)
    report = job.run(MacroSyncOptions(AS_OF, 10, catalog.catalog_version, catalog.catalog_hash, mode="live"))
    outcome = report.outcomes[0]
    assert outcome.status == "fallback" and outcome.actual_source == "bls"
    assert "fred" not in outcome.recovered_sources
    with factory() as session:
        assert session.scalar(select(JobFailure).where(JobFailure.job_run_id == prior_run)).resolved_at is None
    assert reader.visible_observations([SERIES.id], AS_OF)[0].source == "bls"
    assert fallback.calls == []
