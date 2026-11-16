from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.db.models import IngestionBatch, MacroObservation, SourcePayload
from app.macro.jobs.macro_sync_repository import MacroObservationRepository

from backend.tests.jobs.test_macro_sync import AS_OF, CATALOG, TARGET, FakeProvider, old_state, result, setup_job


@pytest.mark.parametrize("status", ["failed", "no_change", "success"])
def test_failed_no_change_and_empty_actual_requests_are_audited_without_core_rows(pg_factory, status: str) -> None:
    fetched = replace(result("fred", status), observations=(), raw_payload={"token": "canary-secret", "response": "controlled"})
    provider = FakeProvider("fred", fetched)
    catalog = CATALOG.model_copy(update={"series": tuple(item if item.id == TARGET else item.model_copy(update={"sources": ()}) for item in CATALOG.series)})
    engine, job, opts, _, _, _, _, _ = setup_job((provider,), catalog=catalog, states=(old_state(TARGET, AS_OF.date() - timedelta(days=2)),))
    factory = pg_factory
    job._observations = MacroObservationRepository(factory)
    try:
        job.run(opts)
        with factory() as session:
            batches = session.scalars(select(IngestionBatch)).all()
            payloads = session.scalars(select(SourcePayload)).all()
            assert len(batches) == len(payloads) == 1
            assert batches[0].request_count == 1
            assert payloads[0].payload["status"] == status
            assert "canary-secret" not in json.dumps(payloads[0].payload)
            assert payloads[0].payload["request"]["window"]["asOf"] == opts.as_of.isoformat()
            assert session.scalars(select(MacroObservation)).all() == []
    finally:
        engine.dispose()
        factory.kw["bind"].dispose()


def test_visible_and_deferred_rows_share_one_request_batch_and_window_hash(pg_factory) -> None:
    fetched = result("fred")
    fetched = replace(fetched, observations=(*fetched.observations, replace(fetched.observations[0], released_at=AS_OF + timedelta(days=1), revision="future")))
    provider = FakeProvider("fred", fetched)
    catalog = CATALOG.model_copy(update={"series": tuple(item if item.id == TARGET else item.model_copy(update={"sources": ()}) for item in CATALOG.series)})
    engine, job, opts, _, _, _, _, _ = setup_job((provider,), catalog=catalog)
    factory = pg_factory
    job._observations = MacroObservationRepository(factory)
    try:
        job.run(opts)
        with factory() as session:
            batches = session.scalars(select(IngestionBatch)).all()
            assert len(batches) == 1
            assert batches[0].row_count == 2 and batches[0].request_count == 1
            assert len(session.scalars(select(SourcePayload)).all()) == 1
            assert len(session.scalars(select(MacroObservation)).all()) == 2
        job.run(replace(opts, as_of=opts.as_of + timedelta(days=7), force=True))
        with factory() as session:
            assert len(set(session.scalars(select(SourcePayload.request_hash)))) == 2
    finally:
        engine.dispose()
        factory.kw["bind"].dispose()


def test_request_retry_identity_is_idempotent_and_raw_survives_core_failure(pg_factory, monkeypatch) -> None:
    from app.connectors.macro.contracts import MacroFetchWindow
    factory = pg_factory
    repository = MacroObservationRepository(factory)
    series = next(item for item in CATALOG.series if item.id == TARGET)
    fetched = replace(result("fred"), request_count=3)
    window = MacroFetchWindow(AS_OF.date() - timedelta(days=31), AS_OF.date(), AS_OF)
    try:
        first = repository.audit_request(series, series.sources[0], window, fetched, AS_OF, "controlled-run")
        second = repository.audit_request(series, series.sources[0], window, fetched, AS_OF, "controlled-run")
        assert first == second
        def fail_core(*args):
            raise RuntimeError("controlled core transaction failure")
        monkeypatch.setattr(repository, "_persist_observation", fail_core)
        with pytest.raises(RuntimeError):
            repository.persist(series, fetched, AS_OF, batch_id=first)
        with factory() as session:
            batches = session.scalars(select(IngestionBatch)).all()
            assert len(batches) == len(session.scalars(select(SourcePayload)).all()) == 1
            assert batches[0].request_count == 3
            assert batches[0].row_count == 0
            assert session.scalars(select(MacroObservation)).all() == []
    finally:
        factory.kw["bind"].dispose()
