from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.connectors.macro.nbs_history import DEFAULT_RELEASES_FILE, NbsReleaseLocator, load_history_releases
from app.connectors.macro.transport import MacroHttpTransport
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_history_backfill import CURSOR_KEY, MacroHistoryBackfillJob, SqlMacroHistoryRuns, macro_history_status
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import bind_history_catalog


@dataclass(frozen=True, slots=True)
class MacroHistoryRuntime:
    job: MacroHistoryBackfillJob
    releases: tuple[NbsReleaseLocator, ...]


def _database(settings: Settings) -> str:
    if not settings.database_url:
        raise ValueError('NBS_HISTORY_DATABASE_REQUIRED')
    return settings.database_url


@contextmanager
def open_macro_history_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                               ) -> Iterator[MacroHistoryRuntime]:
    database_url = _database(settings)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    releases = load_history_releases(settings.macro_backfill_releases_file or DEFAULT_RELEASES_FILE)
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(database_url)
        resources.callback(engine.dispose)
        cursors = SqlJobCursorRepository(factory, job_name='backfill-macro-history')
        catalog = bind_history_catalog(catalog, cursors.load(CURSOR_KEY))
        client = resources.enter_context(httpx.Client(transport=transport,
            timeout=catalog.defaults.request_timeout_seconds, follow_redirects=False))
        # 有界任务不允许 transport 在单个步骤内消耗隐式重试预算。
        http = resources.enter_context(MacroHttpTransport(client=client, max_attempts=1,
            backoff_seconds=catalog.defaults.retry_backoff_seconds))
        yield MacroHistoryRuntime(MacroHistoryBackfillJob(catalog=catalog, transport=http,
            cursors=cursors,
            observations=MacroObservationRepository(factory), history=SqlMacroHistoryRuns(factory),
            lock=job_lock_for_engine(engine)), releases)


def read_macro_history_status(settings: Settings) -> dict[str, object]:
    engine, factory = create_engine_and_session(_database(settings))
    try:
        return macro_history_status(SqlJobCursorRepository(factory, job_name='backfill-macro-history'))
    finally:
        engine.dispose()
