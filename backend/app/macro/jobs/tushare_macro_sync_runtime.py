"""六项已初始化 Tushare 宏观指标的有界增量；独立审计、不自动发布。"""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderTransientError
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.sanitization import sanitize_macro_value
from app.connectors.macro.tushare import TushareMacroProvider
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, state_is_future
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition, MacroSourceDefinition


SERIES_IDS = ('cn.gdp.real_yoy', 'cn.cpi.yoy', 'cn.ppi.yoy', 'cn.pmi.manufacturing',
              'cn.money.m1_yoy', 'cn.money.m2_yoy')
# 类别、频率、单位、季调、freshness、接口、数值列。
_CONTRACTS = (
    ('growth', 'quarterly', '%', 'not_adjusted', 130, 'cn_gdp', 'gdp_yoy'),
    ('inflation', 'monthly', '%', 'not_adjusted', 62, 'cn_cpi', 'nt_yoy'),
    ('inflation', 'monthly', '%', 'not_adjusted', 62, 'cn_ppi', 'ppi_yoy'),
    ('growth', 'monthly', 'index', 'seasonally_adjusted', 62, 'cn_pmi', 'pmi010000'),
    ('money_credit', 'monthly', '%', 'not_adjusted', 62, 'cn_m', 'm1_yoy'),
    ('money_credit', 'monthly', '%', 'not_adjusted', 62, 'cn_m', 'm2_yoy'),
)


def load_active_catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


def _approved_source(api: str, field: str, frequency: str) -> MacroSourceDefinition:
    return MacroSourceDefinition(provider='tushare', priority=1, api=api, field=field,
        transform='identity', source_url='https://tushare.pro/document/2',
        terms_url='https://tushare.pro/document/1?doc_id=1', access_mode='api', display_allowed=True,
        response_fields=('quarter' if frequency == 'quarterly' else 'month', field))


class _LimitedTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, limit: int) -> None:
        self._transport, self._limit, self.used = transport, limit, 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.used >= self._limit:
            raise ProviderTransientError('TUSHARE_MACRO_REQUEST_BUDGET_EXHAUSTED')
        self.used += 1
        try:
            return self._transport.handle_request(request)
        except ProviderTransientError as error:
            if error.code == 'MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED':
                self.used -= 1
            raise

    def close(self) -> None:
        self._transport.close()


class _ScopedProvider(TushareMacroProvider):
    def __init__(self, client: TushareClient, bounded: _LimitedTransport, token: str) -> None:
        super().__init__(client)
        self._bounded, self._token = bounded, token

    def fetch(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
              window: MacroFetchWindow) -> ProviderFetchResult:
        before = self._bounded.used
        result = super().fetch(series, source, window)
        # 通用 provider 计调用尝试；专用审计只计真实 HTTP，预算拦截计零。
        count = self._bounded.used - before
        raw = sanitize_macro_value(result.raw_payload, self._token)
        assert isinstance(raw, dict)
        return replace(result, request_count=count, raw_payload=raw,
            error=result.error.sanitized_copy(request_count=count) if result.error else None)


@contextmanager
def open_tushare_macro_sync_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                                   ) -> Iterator[MacroSyncRuntime]:
    if not settings.database_url:
        raise ValueError('TUSHARE_MACRO_DATABASE_REQUIRED')
    if not settings.tushare_token:
        raise ValueError('TUSHARE_MACRO_TOKEN_REQUIRED')
    catalog = load_active_catalog()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        bounded = resources.enter_context(_LimitedTransport(
            transport if transport is not None else httpx.HTTPTransport(), settings.macro_tushare_request_limit))
        token = settings.tushare_token.get_secret_value()
        rate = (settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip'
                else settings.tushare_standard_requests_per_minute)
        client = resources.enter_context(TushareClient(token, transport=bounded, max_retries=0,
            timeout_seconds=catalog.defaults.request_timeout_seconds, rate_limiter=RequestRateLimiter(rate)))

        def verify(options: MacroSyncOptions) -> None:
            # 共享发布锁内验证；拒绝时不 HTTP、不创建采集 JobRun。
            if (options.series_ids != SERIES_IDS or options.publish or options.mode != 'live'
                    or not options.force or options.history_years != settings.macro_history_years):
                raise ValueError('TUSHARE_MACRO_CONFIG_INVALID')
            end = options.as_of.astimezone(ZoneInfo(settings.sync_timezone)).date()
            states = {s.series_id: s for s in MacroSeriesStateRepository(factory, catalog.catalog_version).get_all()}
            observations = MacroObservationRepository(factory)
            for sid, (category, frequency, unit, seasonal, freshness, api, field) in zip(SERIES_IDS, _CONTRACTS):
                definition = catalog.require(sid)
                if ((definition.region, definition.category, definition.frequency, definition.unit,
                        definition.seasonal_adjustment, definition.display_precision, definition.freshness_days)
                        != ('CN', category, frequency, unit, seasonal, 1, freshness)
                        or definition.sources != (_approved_source(api, field, frequency),)):
                    raise ValueError('TUSHARE_MACRO_SOURCE_INVALID')
                if state_is_future(states.get(sid), options.as_of, end):
                    raise ValueError('TUSHARE_MACRO_CLOCK_INVALID')
                if observations.latest_visible(definition, options.as_of, end, provider='tushare') is None:
                    raise ValueError('TUSHARE_MACRO_CORE_REQUIRED')
            bounded.used = 0

        history = SqlMacroSyncHistory(factory, job_name='sync-tushare-macro')
        job = _build_macro_job(factory, MacroProviderRegistry((_ScopedProvider(client, bounded, token),)),
            engine, catalog, settings.sync_timezone, history=history, before_work=verify, settings=settings)
        yield MacroSyncRuntime(job, catalog)
