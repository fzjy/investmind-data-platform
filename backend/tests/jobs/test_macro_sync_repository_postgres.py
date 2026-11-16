from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import ProviderFetchResult, ProviderObservation
from app.db.models import MacroObservation
from app.macro.jobs.macro_sync_repository import MacroObservationRepository, MacroPersistResult
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


pytestmark = pytest.mark.postgres

CAPTURED_AT = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)


def test_concurrent_persistence_is_idempotent_for_known_and_unknown_revisions(pg_schema_url) -> None:
    known = _observation(
        observed_at=date(2026, 8, 1),
        released_at=datetime(2026, 9, 15, tzinfo=UTC),
        revision="provider-v1",
    )
    known_results = _persist_concurrently(
        pg_schema_url,
        ((known,), (known,)),
        (CAPTURED_AT, CAPTURED_AT),
    )
    assert sorted((result.inserted, result.unchanged) for result in known_results) == [
        (0, 1),
        (1, 0),
    ]
    assert _observation_count(pg_schema_url, known.observed_at) == 1

    unknown = _observation(
        observed_at=date(2026, 7, 1),
        released_at=None,
        revision=None,
    )
    unknown_results = _persist_concurrently(
        pg_schema_url,
        ((unknown,), (unknown,)),
        (CAPTURED_AT, CAPTURED_AT + timedelta(seconds=1)),
    )
    assert sorted((result.inserted, result.unchanged) for result in unknown_results) == [
        (0, 1),
        (1, 0),
    ]
    assert _observation_count(pg_schema_url, unknown.observed_at) == 1


def test_concurrent_reversed_batches_acquire_observation_locks_in_stable_order(pg_schema_url) -> None:
    first = _observation(
        observed_at=date(2026, 6, 1),
        released_at=datetime(2026, 7, 15, tzinfo=UTC),
        revision="provider-v1",
    )
    second = _observation(
        observed_at=date(2026, 5, 1),
        released_at=datetime(2026, 6, 15, tzinfo=UTC),
        revision="provider-v1",
    )

    results = _persist_concurrently(
        pg_schema_url,
        ((first, second), (second, first)),
        (CAPTURED_AT, CAPTURED_AT),
    )

    assert sorted((result.inserted, result.unchanged) for result in results) == [
        (0, 2),
        (2, 0),
    ]
    assert _observation_count(pg_schema_url, first.observed_at) == 1
    assert _observation_count(pg_schema_url, second.observed_at) == 1


def _persist_concurrently(
    database_url: str,
    batches: tuple[tuple[ProviderObservation, ...], tuple[ProviderObservation, ...]],
    captured_at: tuple[datetime, datetime],
) -> tuple[MacroPersistResult, MacroPersistResult]:
    barrier = Barrier(2)
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("us.cpi.yoy")

    def persist(observations, captured):
        engine = _engine(database_url)
        keys = []

        @event.listens_for(engine, "before_cursor_execute")
        def before_lock(_connection, _cursor, statement, parameters, _context, _executemany):
            if "pg_advisory_xact_lock(" in statement:
                keys.append(parameters["lock_key"])
                if len(keys) == 1:
                    barrier.wait(timeout=5)

        try:
            repository = MacroObservationRepository(sessionmaker(engine, expire_on_commit=False))
            return repository.persist(series, _result(*observations), captured), keys
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = [
            executor.submit(persist, observations, captured)
            for observations, captured in zip(batches, captured_at, strict=True)
        ]
        first_result, first_keys = first.result(timeout=12)
        second_result, second_keys = second.result(timeout=12)

    assert len(first_keys) == len(set(first_keys)) == len(batches[0])
    assert first_keys == second_keys
    return first_result, second_result


def _engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        connect_args={
            "options": "-c statement_timeout=5000 -c lock_timeout=5000",
        },
    )


def _observation(
    *,
    observed_at: date,
    released_at: datetime | None,
    revision: str | None,
) -> ProviderObservation:
    return ProviderObservation(
        provider="fred",
        provider_series_id="CPIAUCSL",
        observed_at=observed_at,
        released_at=released_at,
        revision=revision,
        value=Decimal("3.1"),
        source_url="https://example.test/fred/cpi",
    )


def _result(*observations: ProviderObservation) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="fred",
        status="success",
        observations=observations,
        raw_payload={"observations": [{"value": "3.1"}]},
        request_count=1,
    )


def _observation_count(database_url: str, observed_at: date) -> int:
    engine = _engine(database_url)
    factory = sessionmaker(engine)
    try:
        with factory() as session:
            return session.scalar(
                select(func.count())
                .select_from(MacroObservation)
                .where(MacroObservation.observed_at == observed_at)
            ) or 0
    finally:
        engine.dispose()
