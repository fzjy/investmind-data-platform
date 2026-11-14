from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

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
from app.jobs.build_and_publish import BuildAndPublishJob, DefaultMartBuilders
from app.jobs.build_history import SqlBuildHistory
from app.jobs.cursors import SqlJobCursorRepository
from app.stocks.jobs.daily_sync import DailySyncJob
from app.stocks.jobs.daily_sync_history import SqlDailySyncHistory
from app.stocks.jobs.daily_sync_plans import DailySyncPlanBuilder
from app.stocks.jobs.daily_sync_repository import DailySyncRepository
from app.jobs.locks import job_lock_for_engine
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.stocks.universes import UniverseResolver, UniverseRowFilter
from app.stocks.universes.models import UniverseSelection


class DailySyncRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailySyncRuntime:
    job: DailySyncJob
    selection: UniverseSelection
    mode: TushareMode
    session_factory: sessionmaker[Session]


def redact_sensitive_values(message: str, *secrets: str) -> str:
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s]+:)[^@\s]+(@)",
        r"\1***\2",
        redacted,
    )


def redact_runtime_error(message: str, token: str, database_url: str) -> str:
    return redact_sensitive_values(message, token, database_url)


@contextmanager
def open_daily_sync_runtime(
    settings: Settings,
    profile: str,
    universe_file: Path | None,
) -> Iterator[DailySyncRuntime]:
    if not settings.database_url:
        raise DailySyncRuntimeError("sync-daily requires DANG_DATABASE_URL")
    if settings.tushare_token is None:
        raise DailySyncRuntimeError("sync-daily requires DANG_TUSHARE_TOKEN")

    database_url = settings.database_url
    token = settings.tushare_token.get_secret_value()
    mode = TushareMode(settings.tushare_mode)
    engine, factory = create_engine_and_session(database_url)
    try:
        repository = DailySyncRepository(factory)
        selection = UniverseResolver(
            active_symbols=repository.active_symbols,
            security_states=repository.security_states,
        ).resolve(profile, universe_file=universe_file)
        requests_per_minute = (
            settings.tushare_vip_requests_per_minute
            if mode is TushareMode.VIP
            else settings.tushare_standard_requests_per_minute
        )
        limiter = RequestRateLimiter(requests_per_minute)
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
            runner = PaginatedPlanRunner(
                lambda plan: pipeline.run(plan, mode=mode.value),
                SqlJobCursorRepository(factory, job_name="sync-daily"),
                mode=mode,
                page_size=settings.tushare_page_size,
                fingerprint_context=selection.fingerprint,
            )
            build_job = BuildAndPublishJob(
                repository=CoreSnapshotRepository(factory),
                derived=DerivedPipeline(factory),
                builders=DefaultMartBuilders(factory, settings=settings),
                quality=QualityGate(),
                publisher=SnapshotPublisher(factory),
                history=SqlBuildHistory(factory),
                sanitize_error=lambda message: redact_runtime_error(
                    message,
                    token,
                    database_url,
                ),
            )
            job = DailySyncJob(
                runner=runner,
                plan_builder_factory=DailySyncPlanBuilder,
                repository=repository,
                build_job=build_job,
                history=SqlDailySyncHistory(factory),
                lock=job_lock_for_engine(engine),
                publication_lock=job_lock_for_engine(engine),
                universe=selection,
                sanitize_error=lambda message: redact_runtime_error(
                    message,
                    token,
                    database_url,
                ),
            )
            yield DailySyncRuntime(job, selection, mode, factory)
    finally:
        engine.dispose()
