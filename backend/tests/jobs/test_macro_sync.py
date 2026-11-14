from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import select

from app.connectors.macro.contracts import (
    MacroFetchWindow, ProviderError, ProviderFetchResult, ProviderObservation,
    ProviderTransientError,
)
from app.connectors.macro.registry import MacroProviderRegistry
from app.db.base import Base
from app.db.models import JobFailure, JobRun
from app.db.session import create_engine_and_session
from app.jobs.locks import ProcessLocalJobLock
from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.jobs.macro_sync import MacroPublishReport, MacroSyncJob
from app.macro.jobs.macro_sync_history import MacroCatalogVersionHashMismatch, SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.jobs.macro_sync_repository import MacroPersistResult
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition


AS_OF = datetime(2026, 9, 20, 9, tzinfo=UTC)
CATALOG = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
TARGET = "us.rate.treasury_10y"


def observation(
    provider: str, observed_at: date = date(2026, 9, 18),
    released_at: datetime | None = AS_OF - timedelta(days=1),
) -> ProviderObservation:
    return ProviderObservation(
        provider=provider, provider_series_id="test", observed_at=observed_at,
        released_at=released_at, revision="v1", value=Decimal("3.75"),
        source_url="https://example.test/data",
    )


def result(
    provider: str, status: str = "success", *, gap: tuple[str, str] | None = None,
    observed_at: date = date(2026, 9, 18), complete: bool = False,
    released_at: datetime | None = AS_OF - timedelta(days=1),
) -> ProviderFetchResult:
    payload: dict[str, object] = {}
    if gap is not None:
        payload["missingWindows"] = [{"start": gap[0], "end": gap[1]}]
    elif complete:
        payload["missingWindows"] = []
    return ProviderFetchResult(
        provider=provider, status=status,  # type: ignore[arg-type]
        observations=(observation(provider, observed_at, released_at),) if status == "success" else (),
        raw_payload=payload, request_count=1,
        error=ProviderTransientError("SOURCE_FAILED", "api_key=secret-value") if status == "failed" else None,
    )


@dataclass
class FakeProvider:
    provider_id: str
    response: ProviderFetchResult | Exception
    calls: list[tuple[str, MacroFetchWindow]] = field(default_factory=list)
    historical_response: ProviderFetchResult | None = None

    def fetch(self, series: MacroSeriesDefinition, source, window: MacroFetchWindow) -> ProviderFetchResult:
        self.calls.append((series.id, window))
        if self.historical_response is not None and window.end <= date(2020, 1, 3):
            return self.historical_response
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@dataclass
class FakeObservationRepository:
    persisted: list[tuple[str, str]] = field(default_factory=list)
    persisted_rows: list[tuple[str, tuple[ProviderObservation, ...]]] = field(default_factory=list)
    captured_ats: list[datetime] = field(default_factory=list)
    reject_provider: str | None = None
    visible: dict[str, tuple[str, date, datetime]] = field(default_factory=dict)

    def latest_visible(
        self, series: MacroSeriesDefinition, as_of: datetime, end: date,
        *, provider: str | None = None,
    ) -> tuple[str, date, datetime] | None:
        candidates = []
        prior = self.visible.get(series.id)
        if prior is not None and prior[1] <= end and prior[2] <= as_of and (provider is None or prior[0] == provider):
            candidates.append(prior)
        for (series_id, rows), captured_at in zip(self.persisted_rows, self.captured_ats):
            if series_id != series.id:
                continue
            candidates.extend((row.provider, row.observed_at, row.released_at or captured_at)
                for row in rows if row.provider != self.reject_provider and (provider is None or row.provider == provider)
                and row.observed_at <= end and (row.released_at or captured_at) <= as_of
                and (row.valid_until is None or row.valid_until > as_of))
        priorities = {source.provider: source.priority for source in series.sources}
        return max(candidates, key=lambda row: (row[1], -priorities.get(row[0], 999), row[2]), default=None)

    def audit_request(self, series, source, window, result, captured_at, run_id) -> str:
        return "batch"

    def persist(self, series: MacroSeriesDefinition, result: ProviderFetchResult, captured_at: datetime, *, batch_id: str | None = None) -> MacroPersistResult:
        self.persisted.append((series.id, result.provider))
        self.persisted_rows.append((series.id, result.observations))
        self.captured_ats.append(captured_at)
        if result.provider == self.reject_provider:
            return MacroPersistResult(
                batch_id="batch", received=len(result.observations),
                inserted=0, unchanged=0, rejected=len(result.observations),
                latest_observed_at=None, latest_released_at=None,
            )
        return MacroPersistResult(
            batch_id="batch", received=len(result.observations),
            inserted=len(result.observations), unchanged=0, rejected=0,
            latest_observed_at=max((row.observed_at for row in result.observations), default=None),
            latest_released_at=max((row.released_at for row in result.observations if row.released_at), default=None),
        )


@dataclass
class FakeStates:
    initial: tuple[MacroSeriesStateView, ...] = ()
    successes: list[tuple[str, str, bool]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    fail_get: bool = False

    def get_all(self) -> tuple[MacroSeriesStateView, ...]:
        if self.fail_get:
            raise RuntimeError("state store unavailable")
        return self.initial

    def mark_success(self, series_id: str, *, checked_at: datetime, active_source: str,
                     latest_observed_at: date | None, latest_released_at: datetime | None,
                     fallback_used: bool, history_incomplete: bool = False) -> None:
        self.successes.append((series_id, active_source, fallback_used))

    def mark_stale(self, series_id: str, *, checked_at: datetime, error) -> None:
        self.stale.append(series_id)


@dataclass
class FakePublisher:
    blocked: bool = False
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    as_ofs: list[datetime] = field(default_factory=list)

    def publish(self, run_id: str, as_of: datetime, catalog: MacroSeriesCatalog, statuses: tuple[object, ...]) -> MacroPublishReport:
        self.calls.append((run_id, statuses))
        self.as_ofs.append(as_of)
        return MacroPublishReport(
            "blocked" if self.blocked else "completed",
            None if self.blocked else "version-1",
            () if self.blocked else ("macro-transmission", "macro-insights"),
            (), (),
        )


class FailingPublisher(FakePublisher):
    def publish(self, run_id: str, as_of: datetime, catalog: MacroSeriesCatalog, statuses: tuple[object, ...]) -> MacroPublishReport:
        raise RuntimeError("secret_key=publisher-secret")


def old_state(series_id: str, observed_at: date, source: str = "fred") -> MacroSeriesStateView:
    return MacroSeriesStateView(
        series_id=series_id, catalog_version=CATALOG.catalog_version,
        last_checked_at=AS_OF - timedelta(days=7), last_success_at=AS_OF - timedelta(days=7),
        latest_observed_at=observed_at, latest_released_at=AS_OF - timedelta(days=7),
        active_source=source, fallback_used=False, stale=False, stale_reason=None,
        consecutive_failures=0,
    )


def setup_job(
    providers: tuple[FakeProvider, ...], *, states: tuple[MacroSeriesStateView, ...] = (),
    catalog: MacroSeriesCatalog = CATALOG, blocked: bool = False,
):
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observations = FakeObservationRepository()
    state_repository = FakeStates(states)
    publisher = FakePublisher(blocked)
    history = SqlMacroSyncHistory(factory)
    lock = ProcessLocalJobLock()
    job = MacroSyncJob(
        catalog=catalog, registry=MacroProviderRegistry(providers),
        observation_repository=observations, state_repository=state_repository,
        history=history, lock=lock, publisher=publisher, timezone="Asia/Shanghai",
        clock=lambda: AS_OF,
    )
    opts = MacroSyncOptions(AS_OF, 10, catalog.catalog_version, catalog.catalog_hash, mode="live")
    return engine, job, opts, observations, state_repository, publisher, history, lock


def test_official_success_never_calls_fallback() -> None:
    official = FakeProvider("fred", result("fred"))
    fallback = FakeProvider("us_treasury", AssertionError("fallback called"))
    engine, job, opts, observations, states, _, _, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fresh" and target.actual_source == "fred"
        assert not fallback.calls
        assert (TARGET, "fred") in observations.persisted
        assert (TARGET, "fred", False) in states.successes
    finally:
        engine.dispose()


@pytest.mark.parametrize("has_gap", [False, True])
def test_online_primary_replaces_offline_fallback_flag_even_with_history_gap(has_gap):
    from dataclasses import replace
    catalog = CATALOG.model_copy(update={"series": (CATALOG.require(TARGET).model_copy(update={"sources": CATALOG.require(TARGET).sources[:1]}),)})
    official = FakeProvider("fred", result("fred", gap=("2026-08-22", "2026-08-23") if has_gap else None))
    prior = replace(old_state(TARGET, date(2026, 9, 18), "controlled-offline"), fallback_used=True)
    engine, job, opts, observations, states, _, _, _ = setup_job((official,), catalog=catalog, states=(prior,))
    observations.visible[TARGET] = ("controlled-offline", date(2026, 9, 18), AS_OF - timedelta(days=1))
    try:
        outcome = job.run(opts).outcomes[0]
        assert outcome.actual_source == "fred"
        assert outcome.fallback_used is False
        assert outcome.status == ("stale" if has_gap else "fresh")
        assert outcome.stale_reason == ("MACRO_HISTORY_GAP" if has_gap else None)
        if not has_gap:
            assert (TARGET, "fred", False) in states.successes
    finally:
        engine.dispose()


def test_no_change_with_declared_unscanned_history_does_not_claim_coverage_or_recovery():
    catalog = CATALOG.model_copy(update={"series": (CATALOG.require(TARGET).model_copy(update={"sources": CATALOG.require(TARGET).sources[:1]}),)})
    official = FakeProvider("fred", result("fred", "no_change", gap=("2026-08-22", "2026-08-23")))
    engine, job, opts, observations, _, _, _, _ = setup_job((official,), catalog=catalog, states=(old_state(TARGET, date(2026, 9, 18)),))
    observations.visible[TARGET] = ("fred", date(2026, 9, 18), AS_OF - timedelta(days=1))
    try:
        outcome = job.run(opts).outcomes[0]
        assert outcome.status == "stale"
        assert [(gap.start, gap.end) for gap in outcome.missing_windows] == [(date(2026, 8, 22), date(2026, 8, 23))]
        assert outcome.recovered_sources == ()
    finally:
        engine.dispose()


@pytest.mark.parametrize("series_id,provider", [
    ("cn.gdp.real_yoy", "tushare"), ("us.gdp.real_qoq_annualized", "fred"),
])
def test_quarterly_sync_keeps_q2_fresh_in_late_september(series_id, provider):
    definition = CATALOG.require(series_id)
    catalog = CATALOG.model_copy(update={"series": (definition,)})
    engine, job, opts, _, states, _, _, _ = setup_job(
        (FakeProvider(provider, result(provider, observed_at=date(2026, 4, 1))),), catalog=catalog)
    try:
        report = job.run(opts)
        assert report.outcomes[0].status == "fresh"
        assert (series_id, provider, False) in states.successes
        assert states.stale == []
    finally:
        engine.dispose()


def test_sync_older_official_period_does_not_replace_newer_visible_fallback() -> None:
    official = FakeProvider("fred", result("fred", observed_at=date(2026, 9, 16)))
    fallback = FakeProvider("us_treasury", AssertionError("unnecessary fallback request"))
    engine, job, opts, observations, states, _, _, _ = setup_job((official, fallback),
        states=(old_state(TARGET, date(2026, 9, 18), "us_treasury"),))
    observations.visible[TARGET] = ("us_treasury", date(2026, 9, 18), AS_OF - timedelta(days=1))
    try:
        report = job.run(opts)
        selected = next(item for item in report.outcomes if item.series_id == TARGET)
        assert selected.actual_source == "us_treasury"
        assert selected.status == "fallback"
        assert (TARGET, "us_treasury", True) in states.successes
        assert fallback.calls == []
    finally:
        engine.dispose()


def test_manual_sync_conflicts_with_shared_publication_lock_across_week_keys() -> None:
    from app.jobs.locks import data_publication_lock_key
    official = FakeProvider("fred", result("fred"))
    engine, job, opts, observations, _, publisher, _, lock = setup_job((official,))
    contender = ProcessLocalJobLock()
    assert contender.try_acquire(data_publication_lock_key())
    try:
        for offset in (0, 7):
            from dataclasses import replace
            report = job.run(replace(opts, as_of=opts.as_of + timedelta(days=offset)))
            assert report.status == "locked"
        assert official.calls == observations.persisted == publisher.calls == []
    finally:
        contender.release(data_publication_lock_key())
        engine.dispose()


def test_job_uses_configured_local_day_for_iso_week_key_and_window_end() -> None:
    official = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, _, _ = setup_job((official,))
    utc_sunday = datetime(2026, 12, 27, 16, 30, tzinfo=UTC)
    opts = MacroSyncOptions(
        utc_sunday, opts.history_years, opts.catalog_version, opts.catalog_hash,
    )
    try:
        report = job.run(opts)
        assert report.business_key == "2026-W53:" + CATALOG.catalog_version
        target_windows = [window for series_id, window in official.calls if series_id == TARGET]
        assert len(target_windows) == 1
        assert target_windows[0].end == date(2026, 12, 28)
    finally:
        engine.dispose()


def test_explicit_gap_requests_only_missing_subwindow() -> None:
    official = FakeProvider("fred", result("fred", gap=("2026-09-01", "2026-09-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", observed_at=date(2026, 9, 2), complete=True))
    engine, job, opts, observations, _, _, _, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        assert fallback.calls == [(TARGET, MacroFetchWindow(date(2026, 9, 1), date(2026, 9, 3), AS_OF))]
        assert (TARGET, "fred") in observations.persisted
        assert (TARGET, "us_treasury") in observations.persisted
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fallback" and target.actual_source == "fred"
    finally:
        engine.dispose()


def test_historical_fallback_counts_when_official_remains_latest() -> None:
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", observed_at=date(2020, 1, 2), complete=True))
    engine, job, opts, _, states, _, history, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        with history._session_factory() as session:
            run = session.get(JobRun, report.job_run_id)
        assert target.actual_source == "fred"
        assert target.status == "fallback"
        assert target.fallback_used is True
        assert report.fallback == 1
        assert report.to_payload()["fallback"] == 1
        assert (TARGET, "fred", True) in states.successes
        assert run is not None and run.details["fallback"] == 1
    finally:
        engine.dispose()


def test_out_of_window_fallback_observation_does_not_clear_historical_gap() -> None:
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    engine, job, opts, _, states, _, history, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert report.status == "partial" and target.status == "stale"
        assert target.actual_source == "fred"
        assert target.fallback_used is False
        assert target.missing_windows[0].start == date(2020, 1, 1)
        assert report.fallback == 0
        assert TARGET in states.stale
        assert TARGET in history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)
    finally:
        engine.dispose()


def test_one_in_window_fallback_row_without_completeness_declaration_keeps_gap() -> None:
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", observed_at=date(2020, 1, 2)))
    engine, job, opts, _, _, _, history, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert report.status == "partial" and target.status == "stale"
        assert target.missing_windows == (
            MacroFetchWindow(date(2020, 1, 1), date(2020, 1, 3), AS_OF),
        )
        assert history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)[TARGET] == (
            (date(2020, 1, 1), date(2020, 1, 3)),
        )
    finally:
        engine.dispose()


def test_fallback_declared_remaining_subwindow_is_preserved() -> None:
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result(
        "us_treasury", gap=("2020-01-03", "2020-01-03"), observed_at=date(2020, 1, 2),
    ))
    engine, job, opts, _, _, _, _, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.missing_windows == (
            MacroFetchWindow(date(2020, 1, 3), date(2020, 1, 3), AS_OF),
        )
    finally:
        engine.dispose()


def test_unresolved_historical_gap_survives_into_next_week_and_clears_after_fill() -> None:
    old_gap = MacroFetchWindow(date(2020, 1, 1), date(2020, 1, 3), AS_OF)
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", "failed"))
    engine, job, opts, _, states, _, history, _ = setup_job((official, fallback))
    try:
        first = job.run(opts)
        target = next(item for item in first.outcomes if item.series_id == TARGET)
        with history._session_factory() as session:
            first_run = session.get(JobRun, first.job_run_id)
        assert first.status == "partial" and target.status == "stale"
        assert target.missing_windows == (old_gap,)
        assert first_run is not None
        assert first_run.details["unresolvedGaps"][TARGET] == [
            {"start": "2020-01-01", "end": "2020-01-03"}
        ]

        states.initial = (old_state(TARGET, date(2026, 9, 18)),)
        official.response = result("fred", "failed")
        fallback.response = result("us_treasury")
        fallback.historical_response = result("us_treasury", observed_at=date(2020, 1, 2), complete=True)
        next_week = MacroSyncOptions(
            AS_OF + timedelta(days=7), 10, CATALOG.catalog_version, CATALOG.catalog_hash,
        )
        second = job.run(next_week)
        second_target = next(item for item in second.outcomes if item.series_id == TARGET)
        historical_calls = [window for series_id, window in fallback.calls if series_id == TARGET]
        assert historical_calls[-1].start == date(2020, 1, 1)
        assert historical_calls[-1].end == date(2020, 1, 3)
        assert second_target.missing_windows == ()
        with history._session_factory() as session:
            second_run = session.get(JobRun, second.job_run_id)
        assert second_run is not None
        assert TARGET not in second_run.details["unresolvedGaps"]
        assert TARGET not in history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)
    finally:
        engine.dispose()


def test_earlier_as_of_replay_preserves_future_gap_for_next_current_run() -> None:
    official = FakeProvider("fred", result("fred", gap=("2026-09-01", "2026-09-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", "failed"))
    engine, job, opts, _, states, _, history, _ = setup_job((official, fallback))
    try:
        first = job.run(opts)
        assert first.status == "partial"
        official.response = result("fred", observed_at=date(2019, 9, 18))
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            CATALOG.catalog_version, CATALOG.catalog_hash,
        ))
        assert replay.status in ("completed", "partial")
        assert history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)[TARGET] == (
            (date(2026, 9, 1), date(2026, 9, 3)),
        )
        states.initial = (old_state(TARGET, date(2026, 9, 18)),)
        official.response = result("fred", "failed")
        later = job.run(MacroSyncOptions(
            AS_OF + timedelta(days=14), 10, CATALOG.catalog_version, CATALOG.catalog_hash,
        ))
        target_calls = [window for series_id, window in fallback.calls if series_id == TARGET]
        assert later.status == "partial"
        assert target_calls[-1].start == date(2026, 9, 1)
        assert target_calls[-1].end == date(2026, 9, 3)
    finally:
        engine.dispose()


def test_interrupted_retry_preserves_previously_unresolved_gap() -> None:
    official = FakeProvider("fred", result("fred", gap=("2020-01-01", "2020-01-03")))
    fallback = FakeProvider("us_treasury", result("us_treasury", "failed"))
    engine, job, opts, _, states, _, history, _ = setup_job((official, fallback))
    try:
        first = job.run(opts)
        assert TARGET in history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)
        states.fail_get = True
        second = job.run(MacroSyncOptions(
            AS_OF + timedelta(days=7), 10, CATALOG.catalog_version, CATALOG.catalog_hash,
        ))
        assert first.status == "partial" and second.status == "failed"
        assert TARGET in history.unresolved_gaps(CATALOG.catalog_version, CATALOG.catalog_hash)
    finally:
        engine.dispose()


def test_official_failure_uses_fallback_as_fresh_source() -> None:
    official = FakeProvider("fred", result("fred", "failed"))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    engine, job, opts, _, states, _, _, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fallback" and target.actual_source == "us_treasury"
        assert (TARGET, "us_treasury", True) in states.successes
        assert "secret-value" not in str(report.to_payload())
    finally:
        engine.dispose()


def test_existing_official_same_period_outweighs_new_fallback_response() -> None:
    official = FakeProvider("fred", result("fred", "failed"))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    prior = old_state(TARGET, date(2026, 9, 18), source="fred")
    engine, job, opts, observations, states, _, _, _ = setup_job((official, fallback), states=(prior,))
    observations.visible[TARGET] = ("fred", date(2026, 9, 18), AS_OF - timedelta(days=7))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fallback"
        assert target.actual_source == "fred"
        assert target.fallback_used is True
        assert report.fallback >= 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["live", "replay"])
def test_visible_official_same_period_outweighs_current_fallback_state(
    mode: Literal["live", "replay"],
) -> None:
    official = FakeProvider("fred", result("fred", "failed"))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    prior = old_state(TARGET, date(2026, 9, 18), source="us_treasury")
    engine, job, opts, observations, states, _, _, _ = setup_job(
        (official, fallback), states=(prior,),
    )
    observations.visible[TARGET] = (
        "fred", date(2026, 9, 18), AS_OF - timedelta(days=1),
    )
    try:
        run_options = MacroSyncOptions(
            opts.as_of, opts.history_years, opts.catalog_version, opts.catalog_hash,
            mode=mode,
        )
        report = job.run(run_options)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fallback"
        assert target.actual_source == "fred"
        assert target.fallback_used is True
        assert report.fallback == 1
        assert (TARGET, "us_treasury") in observations.persisted
        if mode == "live":
            assert (TARGET, "fred", True) in states.successes
        else:
            assert states.successes == [] and states.stale == []
            assert states.initial == (prior,)
    finally:
        engine.dispose()


def test_existing_fallback_same_period_is_upgraded_by_official_response() -> None:
    official = FakeProvider("fred", result("fred"))
    prior = old_state(TARGET, date(2026, 9, 18), source="us_treasury")
    engine, job, opts, _, _, _, _, _ = setup_job((official,), states=(prior,))
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fresh"
        assert target.actual_source == "fred"
        assert target.fallback_used is False
    finally:
        engine.dispose()


def test_replay_keeps_historically_visible_official_source_on_fallback_tie() -> None:
    official = FakeProvider("fred", result("fred", "failed"))
    fallback = FakeProvider("us_treasury", result(
        "us_treasury", observed_at=date(2019, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC),
    ))
    future = old_state(TARGET, date(2026, 9, 18), source="us_treasury")
    engine, job, opts, observations, states, _, _, _ = setup_job(
        (official, fallback), states=(future,),
    )
    observations.visible[TARGET] = (
        "fred", date(2019, 9, 18), datetime(2019, 9, 19, tzinfo=UTC),
    )
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in replay.outcomes if item.series_id == TARGET)
        assert target.status == "fallback"
        assert target.actual_source == "fred"
        assert target.fallback_used is True
        assert replay.fallback == 1
        assert states.successes == [] and states.stale == []
    finally:
        engine.dispose()


def test_rejected_official_rows_trigger_series_fallback() -> None:
    official = FakeProvider("fred", result("fred"))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    engine, job, opts, observations, _, _, _, _ = setup_job((official, fallback))
    observations.reject_provider = "fred"
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fallback"
        assert target.rejected_count == 1
        assert len(fallback.calls) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("has_old,expected", [(True, "stale"), (False, "missing")])
def test_all_sources_fail_retains_or_marks_missing_and_still_publishes(has_old: bool, expected: str) -> None:
    providers = tuple(FakeProvider(name, result(name, "failed")) for name in ("fred", "us_treasury", "tushare"))
    initial = (old_state(TARGET, date(2026, 9, 18)),) if has_old else ()
    engine, job, opts, _, states, publisher, _, _ = setup_job(providers, states=initial)
    try:
        report = job.run(opts)
        assert next(item for item in report.outcomes if item.series_id == TARGET).status == expected
        assert TARGET in states.stale
        assert report.status == "partial" and len(publisher.calls) == 1
    finally:
        engine.dispose()


def test_no_change_with_expired_old_value_is_stale() -> None:
    provider = FakeProvider("fred", result("fred", "no_change"))
    engine, job, opts, _, states, _, _, _ = setup_job((provider,), states=(old_state(TARGET, date(2026, 8, 1)),))
    try:
        report = job.run(opts)
        assert next(item for item in report.outcomes if item.series_id == TARGET).status == "stale"
        assert (TARGET, 'fred', False) in states.successes
        assert TARGET not in states.stale
    finally:
        engine.dispose()


def test_replay_no_change_never_uses_future_series_state_or_mutates_it() -> None:
    provider = FakeProvider("fred", result("fred", "no_change"))
    future = old_state(TARGET, date(2026, 9, 18))
    engine, job, opts, _, states, _, _, _ = setup_job((provider,), states=(future,))
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in replay.outcomes if item.series_id == TARGET)
        window = next(window for series_id, window in provider.calls if series_id == TARGET)
        assert target.status == "missing" and target.actual_source is None
        assert window.start == date(2009, 9, 20)
        assert states.successes == [] and TARGET not in states.stale
        assert states.initial == (future,)
    finally:
        engine.dispose()


@pytest.mark.parametrize("response,expected", [
    (result("fred", "success"), "fresh"),
    (result("fred", "no_change"), "fresh"),
    (result("fred", "failed"), "stale"),
])
def test_strict_replay_never_updates_even_older_current_state(
    response: ProviderFetchResult, expected: str,
) -> None:
    provider = FakeProvider("fred", response)
    prior = old_state(TARGET, date(2026, 9, 18))
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,), states=(prior,))
    observations.visible[TARGET] = ("fred", date(2026, 9, 18), AS_OF - timedelta(days=7))
    try:
        report = job.run(MacroSyncOptions(
            AS_OF, 10, opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == expected
        assert TARGET not in [series_id for series_id, _, _ in states.successes]
        assert TARGET not in states.stale
        assert states.initial == (prior,)
    finally:
        engine.dispose()


def test_replay_no_change_uses_only_historically_visible_observation() -> None:
    provider = FakeProvider("fred", result("fred", "no_change"))
    future = old_state(TARGET, date(2026, 9, 18), source="us_treasury")
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,), states=(future,))
    observations.visible[TARGET] = (
        "fred", date(2019, 9, 18), datetime(2019, 9, 19, tzinfo=UTC),
    )
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in replay.outcomes if item.series_id == TARGET)
        assert target.status == "fresh" and target.actual_source == "fred"
        assert states.successes == [] and TARGET not in states.stale
    finally:
        engine.dispose()


def test_replay_provider_success_does_not_write_over_future_state() -> None:
    provider = FakeProvider("fred", result(
        "fred", observed_at=date(2019, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC),
    ))
    future = old_state(TARGET, date(2026, 9, 18), source="us_treasury")
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,), states=(future,))
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in replay.outcomes if item.series_id == TARGET)
        assert target.status == "fresh" and target.actual_source == "fred"
        assert (TARGET, "fred") in observations.persisted
        assert TARGET not in [series_id for series_id, _, _ in states.successes]
        assert TARGET not in states.stale
    finally:
        engine.dispose()


def test_replay_excludes_provider_observation_released_after_as_of() -> None:
    provider = FakeProvider("fred", result(
        "fred", observed_at=date(2019, 9, 18),
        released_at=datetime(2026, 9, 19, tzinfo=UTC),
    ))
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,))
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in replay.outcomes if item.series_id == TARGET)
        assert target.status == "missing"
        assert any(
            series_id == TARGET and rows and rows[0].released_at == datetime(2026, 9, 19, tzinfo=UTC)
            for series_id, rows in observations.persisted_rows
        )
        assert TARGET not in states.stale
    finally:
        engine.dispose()


def test_current_run_persists_unknown_release_with_actual_capture_time() -> None:
    provider = FakeProvider("fred", result("fred", released_at=None))
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,))
    captured_at = AS_OF - timedelta(minutes=5)
    job._clock = lambda: captured_at
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fresh"
        assert (TARGET, "fred", False) in states.successes
        assert any(
            series_id == TARGET and rows and rows[0].released_at is None
            for series_id, rows in observations.persisted_rows
        )
        assert set(observations.captured_ats) == {captured_at}
    finally:
        engine.dispose()


def test_live_run_started_before_capture_can_publish_unknown_release() -> None:
    provider = FakeProvider("fred", result("fred", released_at=None))
    engine, job, opts, observations, states, publisher, _, _ = setup_job((provider,))
    captured_at = AS_OF + timedelta(minutes=5)
    job._clock = lambda: captured_at
    try:
        report = job.run(opts)
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fresh"
        assert (TARGET, "fred", False) in states.successes
        assert any(
            series_id == TARGET and rows and rows[0].released_at is None
            for series_id, rows in observations.persisted_rows
        )
        assert set(observations.captured_ats) == {captured_at}
        assert publisher.as_ofs == [captured_at]
        assert report.as_of == AS_OF
        assert report.publication_as_of == captured_at
    finally:
        engine.dispose()


def test_same_local_day_default_is_strict_replay_not_implicit_live() -> None:
    provider = FakeProvider("fred", result(
        "fred", released_at=AS_OF + timedelta(hours=1),
    ))
    engine, job, opts, observations, states, publisher, _, _ = setup_job((provider,))
    job._clock = lambda: AS_OF + timedelta(hours=2)
    try:
        report = job.run(MacroSyncOptions(
            AS_OF, 10, opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "missing"
        assert any(series_id == TARGET and rows for series_id, rows in observations.persisted_rows)
        assert states.successes == []
        assert TARGET not in states.stale
        assert publisher.as_ofs == [AS_OF]
        assert report.publication_as_of == AS_OF
    finally:
        engine.dispose()


def test_explicit_live_mode_uses_capture_cutoff_even_on_a_later_day() -> None:
    provider = FakeProvider("fred", result("fred", released_at=None))
    engine, job, opts, _, states, publisher, _, _ = setup_job((provider,))
    captured_at = AS_OF + timedelta(days=3)
    job._clock = lambda: captured_at
    try:
        report = job.run(MacroSyncOptions(
            AS_OF, 10, opts.catalog_version, opts.catalog_hash, mode="live",
        ))
        target = next(item for item in report.outcomes if item.series_id == TARGET)
        assert target.status == "fresh"
        assert (TARGET, "fred", False) in states.successes
        assert publisher.as_ofs == [captured_at]
    finally:
        engine.dispose()


def test_next_run_can_recover_capture_only_observation_after_cutoff() -> None:
    provider = FakeProvider("fred", result("fred", released_at=None))
    engine, job, opts, observations, states, _, _, _ = setup_job((provider,))
    captured_at = AS_OF + timedelta(days=3, hours=1)
    job._clock = lambda: captured_at
    try:
        first = job.run(MacroSyncOptions(
            AS_OF, 10, opts.catalog_version, opts.catalog_hash,
        ))
        assert next(item for item in first.outcomes if item.series_id == TARGET).status == "missing"
        assert any(
            series_id == TARGET and rows and rows[0].released_at is None
            for series_id, rows in observations.persisted_rows
        )
        observations.visible[TARGET] = ("fred", date(2026, 9, 18), captured_at)
        provider.response = result("fred", "no_change")
        second = job.run(MacroSyncOptions(
            captured_at + timedelta(hours=1), 10, opts.catalog_version, opts.catalog_hash,
            mode="live",
        ))
        target = next(item for item in second.outcomes if item.series_id == TARGET)
        assert target.status == "fresh" and target.actual_source == "fred"
        assert (TARGET, "fred", False) in states.successes
    finally:
        engine.dispose()


def test_replay_uses_real_capture_time_and_does_not_backdate_unknown_release() -> None:
    provider = FakeProvider("fred", result(
        "fred", observed_at=date(2019, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC),
    ))
    engine, job, opts, observations, _, _, _, _ = setup_job((provider,))
    captured_at = datetime(2026, 9, 23, 10, tzinfo=UTC)
    job._clock = lambda: captured_at
    try:
        replay = job.run(MacroSyncOptions(
            datetime(2019, 9, 20, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        assert replay.status in ("completed", "partial")
        assert observations.captured_ats
        assert set(observations.captured_ats) == {captured_at}

        provider.response = result(
            "fred", observed_at=date(2019, 9, 18), released_at=None,
        )
        second = job.run(MacroSyncOptions(
            datetime(2019, 9, 27, 9, tzinfo=UTC), 10,
            opts.catalog_version, opts.catalog_hash,
        ))
        target = next(item for item in second.outcomes if item.series_id == TARGET)
        # The store now retains the first run's genuinely released historical
        # row. It may remain stale, but the unknown-release new row is invisible.
        assert target.status == "stale"
        assert observations.latest_visible(CATALOG.require(TARGET),
            datetime(2019, 9, 27, 9, tzinfo=UTC), date(2019, 9, 27))[2] == datetime(2019, 9, 19, tzinfo=UTC)
        assert any(
            series_id == TARGET and rows and rows[0].released_at is None
            for series_id, rows in observations.persisted_rows
        )
    finally:
        engine.dispose()


def test_publication_blocked_reports_failed_without_success_version() -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, publisher, _, _ = setup_job((provider,), blocked=True)
    try:
        report = job.run(opts)
        assert report.status == "failed" and report.publication_version is None
        assert report.products == ()
        assert report.publication is not None
        assert report.publication.version is None
        assert report.publication.products == ()
        assert len(publisher.calls) == 1
    finally:
        engine.dispose()


def test_publication_exception_is_reported_without_secret_or_version() -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, _, _ = setup_job((provider,))
    job._publisher = FailingPublisher()
    try:
        report = job.run(opts)
        payload = report.to_payload()
        assert report.status == "failed" and report.publication_version is None
        assert {item["code"] for item in payload["failures"]} >= {"MACRO_PUBLICATION_FAILED"}
        assert "publisher-secret" not in str(payload)
    finally:
        engine.dispose()


def test_publisher_owns_quality_recording_after_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, history, _ = setup_job((provider,))

    def unavailable_quality_recorder(*_args: object) -> None:
        raise RuntimeError("quality recorder unavailable after publication")

    monkeypatch.setattr(history, "record_quality", unavailable_quality_recorder)
    try:
        report = job.run(opts)
        assert report.status == "partial"
        assert report.publication_version == "version-1"
    finally:
        engine.dispose()


def test_completed_business_key_skips_provider_and_force_retries() -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, history, _ = setup_job((provider,))
    try:
        key = "2026-W38:" + CATALOG.catalog_version
        run_id = history.start(key, AS_OF, CATALOG.catalog_version, CATALOG.catalog_hash, "cli", 1)
        history.finish(run_id, "completed", {})
        skipped = job.run(opts)
        assert skipped.status == "skipped" and skipped.attempt == 2 and not provider.calls
        forced = job.run(MacroSyncOptions(AS_OF, 10, CATALOG.catalog_version, CATALOG.catalog_hash, force=True))
        assert forced.status != "skipped" and provider.calls
    finally:
        engine.dispose()


def test_held_lock_returns_locked_without_provider_calls() -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, _, lock = setup_job((provider,))
    key = "sync-macro:2026-W38:" + CATALOG.catalog_version
    assert lock.try_acquire(key)
    try:
        assert job.run(opts).status == "locked"
        assert not provider.calls
    finally:
        lock.release(key)
        engine.dispose()


def test_catalog_hash_conflict_is_rejected_before_lock_and_provider() -> None:
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, history, lock = setup_job((provider,))
    business_key = "2026-W38:" + CATALOG.catalog_version
    lock_key = "sync-macro:" + business_key
    history.start(business_key, AS_OF, CATALOG.catalog_version, "b" * 64, "cli", 1)
    assert lock.try_acquire(lock_key)
    try:
        with pytest.raises(MacroCatalogVersionHashMismatch, match="MACRO_CATALOG_VERSION_HASH_MISMATCH"):
            job.run(opts)
        assert not provider.calls
    finally:
        lock.release(lock_key)
        engine.dispose()


def test_series_exception_does_not_stop_other_nineteen() -> None:
    provider = FakeProvider("fred", RuntimeError("secret_key=private-value"))
    engine, job, opts, _, states, publisher, _, _ = setup_job((provider,))
    try:
        report = job.run(opts)
        assert len(report.outcomes) == 20
        assert TARGET not in states.stale and not states.successes
        assert next(item for item in report.outcomes if item.series_id == TARGET).source_failures
        assert len(publisher.calls) == 1
        assert "private-value" not in str(report.to_payload())
    finally:
        engine.dispose()


def test_provider_supplied_error_code_cannot_expose_secret_in_report_or_history() -> None:
    provider = FakeProvider(
        "fred", ProviderError("bare-secret-value", "credential bare-secret-value", retryable=False),
    )
    engine, job, opts, _, _, _, history, _ = setup_job((provider,))
    try:
        report = job.run(opts)
        assert len(report.outcomes) == 20
        assert "bare-secret-value" not in str(report.to_payload())
        with history._session_factory() as session:
            stored = session.scalars(select(JobFailure)).all()
        assert "bare-secret-value" not in str([(item.error_code, item.message) for item in stored])
    finally:
        engine.dispose()


def test_uppercase_provider_error_code_is_never_trusted_as_a_safe_code() -> None:
    token = "FREDTOKENSHOULDNEVERLEAVE"
    provider = FakeProvider("fred", ProviderError(token, token, retryable=False))
    engine, job, opts, _, _, _, history, _ = setup_job((provider,))
    try:
        report = job.run(opts)
        with history._session_factory() as session:
            stored = session.scalars(select(JobFailure)).all()
        assert token not in str(report.failures)
        assert token not in str(report.to_payload())
        assert token not in str([(item.error_code, item.message) for item in stored])
        assert "MACRO_SOURCE_ERROR" in {item.error_code for item in stored}
    finally:
        engine.dispose()


def test_history_keeps_actual_source_and_run_counts() -> None:
    official = FakeProvider("fred", result("fred", "failed"))
    fallback = FakeProvider("us_treasury", result("us_treasury"))
    engine, job, opts, _, _, _, history, _ = setup_job((official, fallback))
    try:
        report = job.run(opts)
        with history._session_factory() as session:
            run = session.get(JobRun, report.job_run_id)
        assert run is not None
        assert run.details["actualSources"][TARGET] == "us_treasury"
        assert run.details["planned"] == 20
        assert run.details["requestCount"] == report.request_count
        with history._session_factory() as session:
            target_failures = session.scalars(
                select(JobFailure).where(JobFailure.dataset == f"macro:{TARGET}")
            ).all()
        assert len(target_failures) == 1
        assert target_failures[0].request_key == "fred"
    finally:
        engine.dispose()


def test_every_item_fresh_completes() -> None:
    source = CATALOG.require(TARGET).sources[0]
    catalog = CATALOG.model_copy(update={
        "series": tuple(series.model_copy(update={"sources": (source,)}) for series in CATALOG.series)
    })
    provider = FakeProvider("fred", result("fred"))
    engine, job, opts, _, _, _, _, _ = setup_job((provider,), catalog=catalog)
    try:
        report = job.run(opts)
        assert report.status == "completed" and report.completed == 20
        assert report.planned == 20 and report.publication_version == "version-1"
        assert report.to_payload()["jobRunId"] == report.job_run_id
    finally:
        engine.dispose()


def test_collection_failure_with_usable_old_value_never_marks_success():
    from dataclasses import replace
    definition = CATALOG.require(TARGET)
    catalog = CATALOG.model_copy(update={'series': (definition,)})
    providers = tuple(FakeProvider(s.provider, result(s.provider, 'failed')) for s in definition.sources)
    prior = old_state(TARGET, date(2026, 9, 18))
    engine, job, opts, observations, states, _, _, _ = setup_job(providers, states=(prior,), catalog=catalog)
    observations.visible[TARGET] = (prior.active_source, prior.latest_observed_at, prior.latest_released_at)
    try:
        job.run(replace(opts, publish=False))
        assert not states.successes
        assert TARGET in states.stale
    finally:
        engine.dispose()


def test_collection_success_is_not_rejected_just_because_observation_is_old():
    from dataclasses import replace
    definition = CATALOG.require(TARGET)
    catalog = CATALOG.model_copy(update={'series': (definition,)})
    providers = tuple(FakeProvider(s.provider, result(s.provider, observed_at=date(2026, 1, 5))) for s in definition.sources)
    engine, job, opts, _, states, _, _, _ = setup_job(providers, catalog=catalog)
    try:
        job.run(replace(opts, publish=False))
        assert states.successes
        assert TARGET not in states.stale
    finally:
        engine.dispose()


def test_no_request_no_change_does_not_overwrite_collection_state():
    from dataclasses import replace
    definition = CATALOG.require(TARGET)
    catalog = CATALOG.model_copy(update={'series': (definition,)})
    providers = tuple(FakeProvider(s.provider, replace(result(s.provider, 'no_change'), request_count=0)) for s in definition.sources)
    prior = old_state(TARGET, date(2026, 9, 18))
    engine, job, opts, observations, states, _, _, _ = setup_job(providers, states=(prior,), catalog=catalog)
    observations.visible[TARGET] = (prior.active_source, prior.latest_observed_at, prior.latest_released_at)
    try:
        job.run(replace(opts, publish=False))
        assert not states.successes and not states.stale
    finally:
        engine.dispose()


@pytest.mark.parametrize('raised', [False, True])
@pytest.mark.parametrize('previous_failed', [False, True])
@pytest.mark.parametrize('has_gap', [False, True])
def test_zero_request_budget_preserves_persisted_collection_times(raised, previous_failed, has_gap):
    from dataclasses import replace
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository

    definition = CATALOG.require(TARGET)
    catalog = CATALOG.model_copy(update={'series': (
        definition.model_copy(update={'sources': definition.sources[:1]}),)})
    error = ProviderTransientError('FRED_MACRO_REQUEST_BUDGET_EXHAUSTED', request_count=0)
    response = error if raised else replace(result('fred', 'failed'), error=error, request_count=0)
    engine, job, opts, observations, _, _, history, _ = setup_job((FakeProvider('fred', response),), catalog=catalog)
    states = MacroSeriesStateRepository(history._session_factory, catalog.catalog_version)
    states.mark_success(TARGET, checked_at=AS_OF-timedelta(days=7), active_source='fred',
        latest_observed_at=date(2026, 9, 18), latest_released_at=AS_OF-timedelta(days=7), fallback_used=False)
    if previous_failed:
        states.mark_stale(TARGET, checked_at=AS_OF-timedelta(days=1), error=ProviderTransientError('MACRO_SOURCE_ERROR'))
    before = states.get_all()
    job._states = states
    observations.visible[TARGET] = ('fred', date(2026, 9, 18), AS_OF-timedelta(days=7))
    # 残留历史缺口不能把本次未执行伪造成一次采集失败。
    if has_gap:
        history.unresolved_gaps = lambda *_: {TARGET: ((date(2020, 1, 1), date(2020, 1, 3)),)}
    try:
        report = job.run(replace(opts, publish=False))
        assert report.request_count == 0
        assert states.get_all() == before
        assert bool(report.outcomes[0].missing_windows) is has_gap
    finally:
        engine.dispose()


def test_real_request_failure_survives_zero_request_fallback_and_history_gap():
    from dataclasses import replace
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository

    catalog = CATALOG.model_copy(update={'series': (CATALOG.require(TARGET),)})
    actual = FakeProvider('fred', ProviderTransientError('MACRO_SOURCE_ERROR', request_count=1))
    skipped = FakeProvider('us_treasury', ProviderTransientError('US_RATES_REQUEST_BUDGET_EXHAUSTED'))
    engine, job, opts, observations, _, _, history, _ = setup_job((actual, skipped), catalog=catalog)
    states = MacroSeriesStateRepository(history._session_factory, catalog.catalog_version)
    states.mark_success(TARGET, checked_at=AS_OF-timedelta(days=7), active_source='fred',
        latest_observed_at=date(2026, 9, 18), latest_released_at=AS_OF-timedelta(days=7), fallback_used=False)
    before = states.get_all()[0]
    job._states = states
    observations.visible[TARGET] = ('fred', date(2026, 9, 18), AS_OF-timedelta(days=7))
    history.unresolved_gaps = lambda *_: {TARGET: ((date(2020, 1, 1), date(2020, 1, 3)),)}
    try:
        report = job.run(replace(opts, publish=False))
        current = states.get_all()[0]
        assert report.request_count > 0
        assert current.last_error_code == 'MACRO_SOURCE_ERROR'
        assert current.last_checked_at == AS_OF
        assert current.last_success_at == before.last_success_at
        assert current.latest_observed_at == before.latest_observed_at
        assert report.outcomes[0].stale_reason == 'MACRO_HISTORY_GAP'
    finally:
        engine.dispose()


def test_collection_success_with_history_gap_keeps_business_quality_stale():
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository

    definition = CATALOG.require(TARGET)
    catalog = CATALOG.model_copy(update={'series': (
        definition.model_copy(update={'sources': definition.sources[:1]}),)})
    provider = FakeProvider('fred', result('fred', gap=('2020-01-01', '2020-01-03')))
    engine, job, opts, _, _, publisher, history, _ = setup_job((provider,), catalog=catalog)
    states = MacroSeriesStateRepository(history._session_factory, catalog.catalog_version)
    job._states = states
    try:
        report = job.run(opts)
        current = states.get_all()[0]
        assert current.last_checked_at == current.last_success_at == AS_OF
        assert current.last_error_code is None
        assert current.stale and current.stale_reason == 'MACRO_HISTORY_GAP'
        assert report.status == 'partial'
        assert report.outcomes[0].stale_reason == 'MACRO_HISTORY_GAP'
        assert TARGET in history.unresolved_gaps(catalog.catalog_version, catalog.catalog_hash)
        assert publisher.calls[0][1] == report.outcomes
        # 独立 rebuild 未携带本轮 outcome 时也必须保留历史质量降级。
        from types import SimpleNamespace
        from app.macro.marts.macro import series_status_payload
        latest = SimpleNamespace(source='fred', observed_at=date(2026, 9, 18),
            released_at=AS_OF-timedelta(days=1), source_url='https://example.test/data')
        quality = series_status_payload(definition, current, latest, AS_OF)
        assert quality['stale'] and quality['staleReason'] == 'MACRO_HISTORY_GAP'
    finally:
        engine.dispose()
