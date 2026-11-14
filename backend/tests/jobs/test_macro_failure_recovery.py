from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.base import ExternalBase
from app.db.models import JobFailure, PublishedSnapshot
from app.jobs.sync_status import MacroStatusReader
from backend.tests.jobs.test_macro_sync import AS_OF, CATALOG, TARGET, FakeProvider, old_state, result, setup_job


@pytest.mark.parametrize("status,mode,want_resolved", [
    ("success", "live", True), ("success", "replay", False),
    ("no_change", "live", False), ("empty", "live", False),
])
def test_source_recovery_resolves_only_its_prior_failure_and_not_replay(status, mode, want_resolved) -> None:
    catalog = CATALOG.model_copy(update={"series": tuple(
        item if item.id == TARGET else item.model_copy(update={"sources": ()}) for item in CATALOG.series
    )})
    fetched = result("fred", "success" if status == "empty" else status)
    if status == "empty":
        fetched = replace(fetched, observations=())
    provider = FakeProvider("fred", fetched)
    engine, job, opts, _, _, _, history, _ = setup_job((provider,), catalog=catalog)
    factory = history._session_factory
    ExternalBase.metadata.create_all(engine)
    PublishedSnapshot.__table__.create(engine, checkfirst=True)
    earlier = AS_OF - timedelta(days=7)
    run_id = history.start("prior", earlier, catalog.catalog_version, catalog.catalog_hash, "test", 1)
    history.record_failure(run_id, TARGET, "fred", "TIMEOUT", "TIMEOUT", True)
    history.record_failure(run_id, TARGET, "us_treasury", "TIMEOUT", "TIMEOUT", True)
    history.record_failure(run_id, "us.cpi.yoy", "fred", "TIMEOUT", "TIMEOUT", True)
    future_id = history.start("future", AS_OF + timedelta(days=7), catalog.catalog_version, catalog.catalog_hash, "test", 1)
    history.record_failure(future_id, TARGET, "fred", "TIMEOUT", "TIMEOUT", True)
    try:
        job.run(replace(opts, mode=mode))
        with factory() as session:
            failures = session.scalars(select(JobFailure).order_by(JobFailure.id)).all()
            assert (failures[0].resolved_at is not None) is want_resolved
            assert all(row.resolved_at is None for row in failures[1:4])
            assert len(failures) >= 4  # Evidence retained, never deleted.
        status_payload = MacroStatusReader(factory, Settings(macro_sync_enabled=True), catalog=catalog).read(AS_OF)
        if want_resolved:
            assert status_payload["openFailures"] == len(failures) - 1
    finally:
        engine.dispose()


def test_confirmed_no_change_with_existing_source_value_can_resolve_timeout() -> None:
    provider = FakeProvider("fred", result("fred", "no_change"))
    engine, job, opts, observations, _, _, history, _ = setup_job((provider,), states=(old_state(TARGET, AS_OF.date()),))
    observations.visible[TARGET] = ("fred", AS_OF.date(), AS_OF - timedelta(days=1))
    run_id = history.start("prior", AS_OF - timedelta(days=7), CATALOG.catalog_version, CATALOG.catalog_hash, "test", 1)
    history.record_failure(run_id, TARGET, "fred", "TIMEOUT", "TIMEOUT", True)
    try:
        job.run(opts)
        with history._session_factory() as session:
            first = session.scalar(select(JobFailure).order_by(JobFailure.id))
            assert first.resolved_at is not None
    finally:
        engine.dispose()


def test_successful_retry_removes_open_failure_from_status_but_keeps_history() -> None:
    catalog = CATALOG.model_copy(update={"series": tuple(
        item.model_copy(update={"sources": item.sources[:1]}) for item in CATALOG.series if item.id == TARGET
    )})
    provider = FakeProvider("fred", result("fred", "failed"))
    engine, job, opts, _, _, _, history, _ = setup_job((provider,), catalog=catalog)
    ExternalBase.metadata.create_all(engine)
    PublishedSnapshot.__table__.create(engine, checkfirst=True)
    try:
        job.run(opts)
        reader = MacroStatusReader(history._session_factory, Settings(macro_sync_enabled=True), catalog=catalog)
        assert reader.read(AS_OF)["openFailures"] > 0
        provider.response = result("fred")
        job.run(replace(opts, force=True))
        status_payload = reader.read(AS_OF)
        assert status_payload["openFailures"] == 0
        assert status_payload["failureSummaries"] == []
        with history._session_factory() as session:
            assert session.scalar(select(JobFailure)) is not None
    finally:
        engine.dispose()


def test_no_change_cannot_resolve_using_only_operational_state_without_visible_core() -> None:
    provider = FakeProvider("fred", result("fred", "no_change"))
    engine, job, opts, _, _, _, history, _ = setup_job((provider,), states=(old_state(TARGET, AS_OF.date()),))
    run_id = history.start("prior", AS_OF - timedelta(days=7), CATALOG.catalog_version, CATALOG.catalog_hash, "test", 1)
    history.record_failure(run_id, TARGET, "fred", "TIMEOUT", "TIMEOUT", True)
    try:
        job.run(opts)
        with history._session_factory() as session:
            assert session.scalar(select(JobFailure).order_by(JobFailure.id)).resolved_at is None
    finally:
        engine.dispose()


def test_trusted_offline_restoration_resolves_only_no_source_not_other_unavailable_errors() -> None:
    catalog = CATALOG.model_copy(update={"series": tuple(
        item.model_copy(update={"sources": ()}) for item in CATALOG.series if item.id == TARGET
    )})
    engine, job, opts, observations, _, _, history, _ = setup_job((), catalog=catalog)
    prior = history.start("prior", AS_OF - timedelta(days=7), catalog.catalog_version, catalog.catalog_hash, "test", 1)
    history.record_failure(prior, TARGET, "unavailable", "MACRO_NO_SOURCE", "MACRO_NO_SOURCE", False)
    history.record_failure(prior, TARGET, "unavailable", "MACRO_SERIES_ERROR", "MACRO_SERIES_ERROR", False)
    observations.visible[TARGET] = ("controlled-offline", AS_OF.date(), AS_OF)
    try:
        job.run(opts)
        with history._session_factory() as session:
            failures = session.scalars(select(JobFailure).order_by(JobFailure.id)).all()
            assert failures[0].resolved_at is not None
            assert failures[1].resolved_at is None
    finally:
        engine.dispose()
