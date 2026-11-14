"""受历史证据门控的仅DR007增量，复用同步算法，独立审计且不自动发布。"""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.connectors.macro.tushare import TushareMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.connectors.macro.tushare_dr007 import CHINA_TIME
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.jobs.dr007_history import CURSOR_KEY, SERIES_ID, Dr007HistoryJob, _load, _end, _shift, load_dr007_source
from app.macro.jobs.dr007_history_repository import Dr007HistoryRepository
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, state_is_future
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import bind_history_catalog


def load_active_catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


@contextmanager
def open_dr007_sync_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                              ) -> Iterator[MacroSyncRuntime]:
    if not settings.database_url:
        raise ValueError('DR007_HISTORY_DATABASE_REQUIRED')
    if not settings.tushare_token:
        raise ValueError('DR007_HISTORY_TOKEN_REQUIRED')
    catalog = load_active_catalog()
    historical_source = load_dr007_source()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        cursors = SqlJobCursorRepository(factory, job_name='backfill-dr007-history')
        lock = job_lock_for_engine(engine)

        def verify(options: MacroSyncOptions) -> None:
            # MacroSyncJob 在共享publication锁内调用，尚未写JobRun或发送HTTP。
            if (options.series_ids != (SERIES_ID,) or options.publish or options.mode != 'live'
                    or options.history_years != settings.macro_history_years):
                raise ValueError('DR007_HISTORY_CONFIG_INVALID')
            raw = cursors.load(CURSOR_KEY)
            frozen = bind_history_catalog(catalog, raw)
            checkpoint = _load(cursors)
            if checkpoint is None:
                raise ValueError('DR007_HISTORY_INCOMPLETE')
            if (checkpoint.history_years != settings.macro_history_years
                    or checkpoint.end != (settings.macro_backfill_end or
                        _end(_shift(checkpoint.as_of.astimezone(CHINA_TIME).date(),-1)))):
                raise ValueError('DR007_HISTORY_CONFIG_MISMATCH')
            if checkpoint.as_of > options.as_of or any(entry.captured_at is not None
                    and entry.captured_at > options.as_of for entry in checkpoint.entries):
                raise ValueError('DR007_HISTORY_CLOCK_INVALID')
            definition = catalog.require(SERIES_ID)
            sources = definition.sources
            if (definition.model_dump(exclude={'sources'}) != frozen.require(SERIES_ID).model_dump(exclude={'sources'})
                    or len(sources) != 1 or sources[0].model_dump(exclude={'params'})
                    != historical_source.model_dump(exclude={'params'})):
                raise ValueError('DR007_HISTORY_SOURCE_INVALID')
            pages = sources[0].params.get('maxPages', '')
            if (set(sources[0].params) != {'ts_code','maxPages'}
                    or sources[0].params.get('ts_code') != 'DR007.IB' or not pages.isascii()
                    or not pages.isdecimal() or not 1 <= int(pages) <= 240):
                raise ValueError('DR007_HISTORY_SOURCE_INVALID')
            status = Dr007HistoryJob(catalog=frozen, source=historical_source, transport=None,
                cursors=cursors, repository=Dr007HistoryRepository(factory), lock=lock).status()
            if status['status'] != 'completed':
                raise ValueError('DR007_HISTORY_INCOMPLETE')
            end = options.as_of.astimezone(ZoneInfo(settings.sync_timezone)).date()
            current = next((s for s in MacroSeriesStateRepository(factory, catalog.catalog_version).get_all()
                if s.series_id == SERIES_ID), None)
            if state_is_future(current, options.as_of, end):
                raise ValueError('DR007_HISTORY_CLOCK_INVALID')

        rate = (settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip'
            else settings.tushare_standard_requests_per_minute)
        client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
            transport=transport,max_retries=0,rate_limiter=RequestRateLimiter(rate)))
        history = SqlMacroSyncHistory(factory, job_name='sync-dr007',
            resolution_job_names=('sync-dr007', 'sync-macro'))
        job = _build_macro_job(factory, MacroProviderRegistry((TushareMacroProvider(client),)),
            engine, catalog, settings.sync_timezone, history=history, before_work=verify, settings=settings)
        yield MacroSyncRuntime(job, catalog)
