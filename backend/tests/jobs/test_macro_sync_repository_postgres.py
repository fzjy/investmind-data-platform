from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier, BrokenBarrierError

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
        known,
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
        unknown,
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

    results = _persist_batches_concurrently(
        pg_schema_url,
        ((first, second), (second, first)),
    )

    assert sorted((result.inserted, result.unchanged) for result in results) == [
        (0, 2),
        (2, 0),
    ]
    assert _observation_count(pg_schema_url, first.observed_at) == 1
    assert _observation_count(pg_schema_url, second.observed_at) == 1


def _persist_concurrently(
    database_url: str,
    observation: ProviderObservation,
    captured_at: tuple[datetime, datetime],
) -> tuple[MacroPersistResult, MacroPersistResult]:
    engines = (_engine(database_url), _engine(database_url))
    factories = tuple(
        sessionmaker(engine, expire_on_commit=False) for engine in engines
    )
    barrier = Barrier(2)

    def pause_after_identity_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "SELECT" not in statement or "macro_observation" not in statement:
            return
        try:
            barrier.wait(timeout=1.5)
        except BrokenBarrierError:
            pass

    for engine in engines:
        event.listen(engine, "before_cursor_execute", pause_after_identity_query)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(
                    MacroObservationRepository(factory).persist,
                    MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(
                        "us.cpi.yoy"
                    ),
                    _result(observation),
                    captured,
                )
                for factory, captured in zip(factories, captured_at, strict=True)
            )
            results = tuple(future.result(timeout=8) for future in futures)
            return results[0], results[1]
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", pause_after_identity_query)
            engine.dispose()


def _persist_batches_concurrently(
    database_url: str,
    batches: tuple[
        tuple[ProviderObservation, ProviderObservation],
        tuple[ProviderObservation, ProviderObservation],
    ],
) -> tuple[MacroPersistResult, MacroPersistResult]:
    engines = (_engine(database_url), _engine(database_url))
    factories = tuple(
        sessionmaker(engine, expire_on_commit=False) for engine in engines
    )
    identity_barrier = Barrier(2)

    def synchronize_after_first_lock(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "SELECT" not in statement or "macro_observation" not in statement:
            return
        try:
            identity_barrier.wait(timeout=1.5)
        except BrokenBarrierError:
            pass

    for engine in engines:
        event.listen(engine, "before_cursor_execute", synchronize_after_first_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(
                    MacroObservationRepository(factory).persist,
                    MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(
                        "us.cpi.yoy"
                    ),
                    _result(*observations),
                    CAPTURED_AT,
                )
                for factory, observations in zip(factories, batches, strict=True)
            )
            results = tuple(future.result(timeout=8) for future in futures)
            return results[0], results[1]
    finally:
        for engine in engines:
            event.remove(
                engine,
                "before_cursor_execute",
                synchronize_after_first_lock,
            )
            engine.dispose()


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
