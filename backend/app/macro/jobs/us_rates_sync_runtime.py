"""仅两项已初始化美国利率的有界 FRED 增量；独立审计、不自动发布。"""
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.connectors.macro.contracts import ProviderTransientError
from app.connectors.macro.fred import CalendarFredMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.transport import JsonTransportResponse, MacroHttpTransport
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, state_is_future
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


SERIES_IDS = ('us.rate.effective_fed_funds', 'us.rate.treasury_10y')
_FRED_SERIES = ('DFF', 'DGS10')


def _approved_source(fred_id: str) -> MacroSourceDefinition:
    return MacroSourceDefinition(provider='fred', priority=1, series=fred_id,
        api='series_observations', field='value', transform='identity',
        source_url='https://api.stlouisfed.org/fred/series/observations',
        terms_url='https://fred.stlouisfed.org/docs/api/terms_of_use.html',
        access_mode='api', display_allowed=True,
        response_fields=('date', 'value', 'realtime_start', 'realtime_end'))


def load_active_catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


class _LimitedFredTransport:
    def __init__(self, http: MacroHttpTransport, limit: int) -> None:
        self._http, self._limit, self._used = http, limit, 0

    def reset(self) -> None:
        self._used = 0

    def get_json(self, url: str, *, params: Mapping[str, str] | None = None,
                 headers: Mapping[str, str] | None = None) -> JsonTransportResponse:
        if self._used >= self._limit:
            raise ProviderTransientError('US_RATES_REQUEST_BUDGET_EXHAUSTED')
        self._used += 1
        # 底层零重试，每次调用最多一次真实请求；分页同样消耗共享预算。
        return self._http.get_json(url, params=params, headers=headers)


@contextmanager
def open_us_rates_sync_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                               ) -> Iterator[MacroSyncRuntime]:
    if not settings.database_url:
        raise ValueError('US_RATES_DATABASE_REQUIRED')
    if not settings.fred_api_key:
        raise ValueError('US_RATES_KEY_REQUIRED')
    catalog = load_active_catalog()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        client = resources.enter_context(httpx.Client(transport=transport,
            timeout=catalog.defaults.request_timeout_seconds, follow_redirects=False))
        http = resources.enter_context(MacroHttpTransport(client=client, max_attempts=1,
            backoff_seconds=catalog.defaults.retry_backoff_seconds))
        bounded = _LimitedFredTransport(http, settings.macro_us_rates_request_limit)

        def verify(options: MacroSyncOptions) -> None:
            # 位于共享发布锁内；验证不通过时无HTTP，也不创建采集JobRun。
            if (options.series_ids != SERIES_IDS or options.publish or options.mode != 'live'
                    or not options.force or options.history_years != settings.macro_history_years):
                raise ValueError('US_RATES_CONFIG_INVALID')
            end = options.as_of.astimezone(ZoneInfo(settings.sync_timezone)).date()
            states = {r.series_id: r for r in MacroSeriesStateRepository(factory,
                catalog.catalog_version).get_all()}
            observations = MacroObservationRepository(factory)
            for sid, fred_id in zip(SERIES_IDS, _FRED_SERIES):
                definition = catalog.require(sid)
                primary = min(definition.sources, key=lambda r: r.priority) if definition.sources else None
                if ((definition.region, definition.category, definition.frequency, definition.unit,
                        definition.seasonal_adjustment, definition.display_precision, definition.freshness_days)
                        != ('US', 'rates_fx', 'daily', '%', 'unknown', 2, 7)
                        or primary != _approved_source(fred_id)
                        or sum(s.provider == 'fred' for s in definition.sources) != 1):
                    raise ValueError('US_RATES_SOURCE_INVALID')
                if state_is_future(states.get(sid), options.as_of, end):
                    raise ValueError('US_RATES_CLOCK_INVALID')
                if observations.latest_visible(definition, options.as_of, end, provider='fred') is None:
                    raise ValueError('US_RATES_CORE_REQUIRED')
            bounded.reset()

        # 专用入口只用已登记FRED主源；不构建其他供应商客户端或隐藏回退。
        # 通用sync-macro仍保留原目录及回退行为。
        # 审计仍绑定原目录身份，专用执行视图只含核定主源，绝不进入备用来源。
        execution_catalog = catalog.model_copy(update={'series': tuple(
            s.model_copy(update={'sources': (_approved_source(_FRED_SERIES[SERIES_IDS.index(s.id)]),)})
            if s.id in SERIES_IDS else s for s in catalog.series)})
        providers = (CalendarFredMacroProvider(settings.fred_api_key.get_secret_value(), transport=bounded),)
        history = SqlMacroSyncHistory(factory, job_name='sync-us-rates')
        job = _build_macro_job(factory, MacroProviderRegistry(providers), engine, execution_catalog,
            settings.sync_timezone, history=history, before_work=verify, settings=settings)
        yield MacroSyncRuntime(job, catalog)
