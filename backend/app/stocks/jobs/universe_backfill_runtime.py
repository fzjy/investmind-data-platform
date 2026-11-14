from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.connectors.tushare.strategies import TushareMode
from app.db.session import create_engine_and_session
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.pipeline import DerivedPipeline
from app.ingestion.pagination import PaginatedPlanRunner
from app.ingestion.pipeline import IngestionPipeline
from app.stocks.jobs.backfill import BackfillJob
from app.jobs.build_and_publish import BuildAndPublishJob, DefaultMartBuilders
from app.jobs.build_history import SqlBuildHistory
from app.jobs.cursors import SqlJobCursorRepository
from app.stocks.jobs.daily_sync_repository import DailySyncRepository
from app.stocks.jobs.daily_sync_runtime import redact_runtime_error
from app.jobs.locks import job_lock_for_engine
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillJob,
    UniverseBackfillOptions,
    UniverseBackfillPlanner,
)
from app.stocks.jobs.universe_backfill_history import SqlUniverseBackfillHistory
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.stocks.universes import UniverseResolver, UniverseRowFilter
from app.stocks.universes.models import UniverseSelection


class UniverseBackfillRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class UniverseBackfillRuntime:
    job: UniverseBackfillJob
    options: UniverseBackfillOptions
    selection: UniverseSelection
    mode: TushareMode
    session_factory: sessionmaker[Session]


@contextmanager
def open_universe_backfill_runtime(
    settings: Settings,
) -> Iterator[UniverseBackfillRuntime]:
    if not settings.database_url:
        raise UniverseBackfillRuntimeError(
            "backfill-universe requires DANG_DATABASE_URL"
        )
    if settings.tushare_token is None:
        raise UniverseBackfillRuntimeError(
            "backfill-universe requires DANG_TUSHARE_TOKEN"
        )
    if settings.backfill_as_of is None:
        raise UniverseBackfillRuntimeError(
            "backfill-universe requires DANG_BACKFILL_AS_OF"
        )

    database_url = settings.database_url
    token = settings.tushare_token.get_secret_value()
    mode = TushareMode(settings.tushare_mode)
    engine, factory = create_engine_and_session(database_url)
    try:
        repository = DailySyncRepository(factory)
        selection = UniverseResolver(
            active_symbols=repository.active_symbols,
            security_states=repository.security_states,
        ).resolve(
            settings.sync_universe,
            universe_file=settings.sync_universe_file,
        )
        options = UniverseBackfillOptions(
            as_of=settings.backfill_as_of,
            mode=mode,
            selection=selection,
            market_years=settings.backfill_market_years,
            financial_start=settings.backfill_financial_start,
            batch_size=settings.backfill_batch_size,
        )
        requests_per_minute = (
            settings.tushare_vip_requests_per_minute
            if mode is TushareMode.VIP
            else settings.tushare_standard_requests_per_minute
        )
        limiter = RequestRateLimiter(requests_per_minute)
        sanitize_error = lambda message: redact_runtime_error(
            message,
            token,
            database_url,
        )

        with ExitStack() as resources:
            provider_client = TushareClient(token, rate_limiter=limiter)
            client_closed = False

            def close_client_once() -> None:
                nonlocal client_closed
                if client_closed:
                    return
                client_closed = True
                provider_client.close()

            resources.callback(close_client_once)
            client = provider_client.__enter__()
            pipeline = IngestionPipeline(
                client,
                factory,
                row_filter=UniverseRowFilter(selection),
            )

            def batch_runner_factory(cursor_job_name: str) -> BackfillJob:
                runner = PaginatedPlanRunner(
                    lambda plan: pipeline.run(plan, mode=mode.value),
                    SqlJobCursorRepository(
                        factory,
                        job_name=cursor_job_name,
                    ),
                    mode=mode,
                    page_size=settings.tushare_page_size,
                    fingerprint_context=selection.fingerprint,
                )
                return BackfillJob(
                    runner,
                    sanitize_error=sanitize_error,
                )

            build_job = BuildAndPublishJob(
                repository=CoreSnapshotRepository(factory),
                derived=DerivedPipeline(factory),
                builders=DefaultMartBuilders(factory, settings=settings),
                quality=QualityGate(),
                publisher=SnapshotPublisher(factory),
                history=SqlBuildHistory(factory),
                sanitize_error=sanitize_error,
            )
            job = UniverseBackfillJob(
                planner=UniverseBackfillPlanner(),
                history=SqlUniverseBackfillHistory(factory),
                batch_runner_factory=batch_runner_factory,
                publisher=build_job,
                run_lock=job_lock_for_engine(engine),
                publication_lock=job_lock_for_engine(engine),
                clock=lambda: datetime.now(UTC),
                sanitize_error=sanitize_error,
            )
            yield UniverseBackfillRuntime(
                job=job,
                options=options,
                selection=selection,
                mode=mode,
                session_factory=factory,
            )
    finally:
        engine.dispose()
