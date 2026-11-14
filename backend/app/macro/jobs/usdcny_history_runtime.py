from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.connectors.macro.transport import MacroHttpTransport
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.jobs.usdcny_history import CURSOR_KEY, UsdcnyHistoryJob, load_usdcny_source
from app.macro.jobs.usdcny_history_repository import UsdcnyHistoryRepository
from app.jobs.locks import job_lock_for_engine
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import bind_history_catalog


@dataclass(frozen=True, slots=True)
class UsdcnyHistoryRuntime:
    job: UsdcnyHistoryJob


def _database(settings: Settings) -> str:
    if not settings.database_url:
        raise ValueError('USDCNY_HISTORY_DATABASE_REQUIRED')
    return settings.database_url


@contextmanager
def open_usdcny_history_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                                ) -> Iterator[UsdcnyHistoryRuntime]:
    database_url = _database(settings)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    source = load_usdcny_source()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(database_url)
        resources.callback(engine.dispose)
        cursors = SqlJobCursorRepository(factory, job_name='backfill-usdcny-history')
        catalog = bind_history_catalog(catalog, cursors.load(CURSOR_KEY))
        client = resources.enter_context(httpx.Client(transport=transport,
            timeout=catalog.defaults.request_timeout_seconds, follow_redirects=False))
        http = resources.enter_context(MacroHttpTransport(client=client, max_attempts=1,
            backoff_seconds=catalog.defaults.retry_backoff_seconds))
        yield UsdcnyHistoryRuntime(UsdcnyHistoryJob(catalog=catalog, source=source, transport=http,
            cursors=cursors,
            repository=UsdcnyHistoryRepository(factory), lock=job_lock_for_engine(engine)))


def read_usdcny_history_status(settings: Settings) -> dict[str, object]:
    database_url = _database(settings)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    source = load_usdcny_source()
    engine, factory = create_engine_and_session(database_url)
    try:
        cursors = SqlJobCursorRepository(factory, job_name='backfill-usdcny-history')
        catalog = bind_history_catalog(catalog, cursors.load(CURSOR_KEY))
        return UsdcnyHistoryJob(catalog=catalog, source=source, transport=None,
            cursors=cursors,
            repository=UsdcnyHistoryRepository(factory), lock=job_lock_for_engine(engine)).status()
    finally:
        engine.dispose()
