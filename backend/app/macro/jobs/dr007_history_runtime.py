"""DR007 独立历史 runtime；只读状态不创建供应商客户端。"""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.jobs.dr007_history import CURSOR_KEY, Dr007HistoryJob, load_dr007_source
from app.macro.jobs.dr007_history_repository import Dr007HistoryRepository
from app.jobs.locks import job_lock_for_engine
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import bind_history_catalog


@dataclass(frozen=True, slots=True)
class Dr007HistoryRuntime:
    job: Dr007HistoryJob


def _database(settings: Settings) -> str:
    if not settings.database_url:
        raise ValueError('DR007_HISTORY_DATABASE_REQUIRED')
    return settings.database_url


def _job(settings, factory, engine, transport):
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    cursors=SqlJobCursorRepository(factory,job_name='backfill-dr007-history')
    catalog=bind_history_catalog(catalog,cursors.load(CURSOR_KEY))
    return Dr007HistoryJob(catalog=catalog,source=load_dr007_source(),transport=transport,
        cursors=cursors,repository=Dr007HistoryRepository(factory),lock=job_lock_for_engine(engine))


@contextmanager
def open_dr007_history_runtime(settings: Settings, *,transport: httpx.BaseTransport | None=None
                               ) -> Iterator[Dr007HistoryRuntime]:
    url=_database(settings)
    if not settings.tushare_token:
        raise ValueError('DR007_HISTORY_TOKEN_REQUIRED')
    with ExitStack() as resources:
        engine,factory=create_engine_and_session(url)
        resources.callback(engine.dispose)
        rate=(settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip'
            else settings.tushare_standard_requests_per_minute)
        client=resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
            transport=transport,max_retries=0,rate_limiter=RequestRateLimiter(rate)))
        yield Dr007HistoryRuntime(_job(settings,factory,engine,client))


def read_dr007_history_status(settings: Settings) -> dict[str,object]:
    engine,factory=create_engine_and_session(_database(settings))
    try:
        return _job(settings,factory,engine,None).status()
    finally:
        engine.dispose()
