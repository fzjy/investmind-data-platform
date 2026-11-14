"""两项已初始化 NBS 月频指标的有界增量；独立审计、不自动发布。"""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.connectors.macro.contracts import ProviderTransientError
from app.connectors.macro.nbs import NbsMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.transport import MacroHttpTransport, TextTransportResponse
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, state_is_future
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


SERIES_IDS = ('cn.cpi.core_yoy', 'cn.industry.value_added_yoy')
_CONTRACTS = (('inflation', '不包括食品和能源', '同比涨跌幅（%）'),
              ('growth', '规模以上工业增加值', '同比实际增长（%）'))


def load_active_catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


def _approved_source(series: str, field: str) -> MacroSourceDefinition:
    return MacroSourceDefinition(provider='nbs', priority=1, series=series, field=field,
        transform='identity', source_url='https://www.stats.gov.cn/sj/zxfb/',
        terms_url='https://www.stats.gov.cn/', access_mode='public_page', display_allowed=True,
        params={'maxIndexPages': '24'}, response_fields=('ArticleTitle', 'PubDate'))


class _LimitedNbsTransport:
    def __init__(self, http: MacroHttpTransport, limit: int) -> None:
        self._http, self._limit, self._used = http, limit, 0

    def reset(self) -> None:
        self._used = 0

    def get_text(self, url: str) -> TextTransportResponse:
        if self._used >= self._limit:
            raise ProviderTransientError('NBS_SYNC_REQUEST_BUDGET_EXHAUSTED')
        self._used += 1
        # 零重试，两个指标及其索引/报告/缺口请求共同消费预算。
        return self._http.get_text(url)


@contextmanager
def open_nbs_sync_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                          ) -> Iterator[MacroSyncRuntime]:
    if not settings.database_url:
        raise ValueError('NBS_SYNC_DATABASE_REQUIRED')
    catalog = load_active_catalog()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        client = resources.enter_context(httpx.Client(transport=transport,
            timeout=catalog.defaults.request_timeout_seconds, follow_redirects=False))
        http = resources.enter_context(MacroHttpTransport(client=client, max_attempts=1,
            backoff_seconds=catalog.defaults.retry_backoff_seconds))
        bounded = _LimitedNbsTransport(http, settings.macro_nbs_request_limit)

        def verify(options: MacroSyncOptions) -> None:
            # 共享发布锁内验证；拒绝时不HTTP、不创建采集JobRun。
            if (options.series_ids != SERIES_IDS or options.publish or options.mode != 'live'
                    or not options.force or options.history_years != settings.macro_history_years):
                raise ValueError('NBS_SYNC_CONFIG_INVALID')
            end = options.as_of.astimezone(ZoneInfo(settings.sync_timezone)).date()
            states = {s.series_id: s for s in MacroSeriesStateRepository(factory, catalog.catalog_version).get_all()}
            observations = MacroObservationRepository(factory)
            for sid, (category, identifier, field) in zip(SERIES_IDS, _CONTRACTS):
                definition = catalog.require(sid)
                if ((definition.region, definition.category, definition.frequency, definition.unit,
                        definition.seasonal_adjustment, definition.display_precision, definition.freshness_days)
                        != ('CN', category, 'monthly', '%', 'not_adjusted', 1, 62)
                        or definition.sources != (_approved_source(identifier, field),)):
                    raise ValueError('NBS_SYNC_SOURCE_INVALID')
                if state_is_future(states.get(sid), options.as_of, end):
                    raise ValueError('NBS_SYNC_CLOCK_INVALID')
                if observations.latest_visible(definition, options.as_of, end, provider='nbs') is None:
                    raise ValueError('NBS_SYNC_CORE_REQUIRED')
            bounded.reset()

        # 原目录身份保留用于审计；专用近期执行视图不修改磁盘目录/通用回看配置。
        overlap = catalog.defaults.overlap.model_copy(update={'monthly_months': settings.macro_nbs_lookback_months})
        execution_catalog = catalog.model_copy(update={'defaults': catalog.defaults.model_copy(update={'overlap': overlap})})
        history = SqlMacroSyncHistory(factory, job_name='sync-nbs')
        job = _build_macro_job(factory, MacroProviderRegistry((NbsMacroProvider(bounded),)),
            engine, execution_catalog, settings.sync_timezone, history=history, before_work=verify, settings=settings)
        yield MacroSyncRuntime(job, catalog)
