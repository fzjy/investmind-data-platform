"""六项已初始化 FRED 宏观增量；保留登记的 BLS 回退，共享预算、不自动发布。"""
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.connectors.macro.bls import BlsMacroProvider
from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderTransientError
from app.connectors.macro.fred import CalendarFredMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.sanitization import sanitize_macro_value
from app.connectors.macro.transport import JsonTransportResponse, MacroHttpTransport
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, state_is_future
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime, _build_macro_job
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition, MacroSourceDefinition


SERIES_IDS = ('us.gdp.real_qoq_annualized', 'us.cpi.yoy', 'us.cpi.core_yoy', 'us.pce.core_yoy',
              'us.labor.unemployment_rate', 'us.labor.nonfarm_payroll_change')
# 类别、频率、单位、精度、freshness、FRED标识/变换、可选已登记BLS标识。
_CONTRACTS = (
    ('growth', 'quarterly', '%', 1, 130, 'A191RL1Q225SBEA', 'identity', None),
    ('inflation', 'monthly', '%', 1, 62, 'CPIAUCSL', 'pc1', 'CUSR0000SA0'),
    ('inflation', 'monthly', '%', 1, 62, 'CPILFESL', 'pc1', 'CUSR0000SA0L1E'),
    ('inflation', 'monthly', '%', 1, 62, 'PCEPILFE', 'pc1', None),
    ('labor', 'monthly', '%', 1, 62, 'UNRATE', 'identity', 'LNS14000000'),
    ('labor', 'monthly', 'thousand persons', 0, 62, 'PAYEMS', 'chg', 'CES0000000001'),
)


def load_active_catalog() -> MacroSeriesCatalog:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)


def _approved_sources(fred_id: str, transform: str, bls_id: str | None) -> tuple[MacroSourceDefinition, ...]:
    primary = MacroSourceDefinition(provider='fred', priority=1, series=fred_id,
        api='series_observations', field='value', transform=transform,
        source_url='https://api.stlouisfed.org/fred/series/observations',
        terms_url='https://fred.stlouisfed.org/docs/api/terms_of_use.html',
        access_mode='api', display_allowed=True,
        response_fields=('date', 'value', 'realtime_start', 'realtime_end'))
    if bls_id is None:
        return (primary,)
    fallback = MacroSourceDefinition(provider='bls', priority=2, series=bls_id,
        api='publicAPI/v1/timeseries/data', field='value', transform=transform,
        source_url='https://api.bls.gov/publicAPI/v1/timeseries/data/',
        terms_url='https://www.bls.gov/developers/termsOfService.htm', access_mode='api', display_allowed=True,
        response_fields=('status', 'Results[].series[].seriesID', 'Results[].series[].data.year',
                         'Results[].series[].data.period', 'Results[].series[].data.value'))
    return (primary, fallback)


class _LimitedTransport:
    def __init__(self, http: MacroHttpTransport, limit: int) -> None:
        self._http, self._limit, self._used = http, limit, 0

    def reset(self) -> None:
        self._used = 0

    def _take(self) -> None:
        if self._used >= self._limit:
            raise ProviderTransientError('FRED_MACRO_REQUEST_BUDGET_EXHAUSTED')
        self._used += 1

    def get_json(self, url: str, *, params: Mapping[str, str] | None = None,
                 headers: Mapping[str, str] | None = None) -> JsonTransportResponse:
        self._take()
        return self._http.get_json(url, params=params, headers=headers)

    def post_json(self, url: str, payload: Mapping[str, object], *,
                  headers: Mapping[str, str] | None = None) -> JsonTransportResponse:
        self._take()
        return self._http.post_json(url, payload, headers=headers)


class _SafeBlsProvider(BlsMacroProvider):
    def __init__(self, transport: _LimitedTransport, key: str) -> None:
        super().__init__(transport)
        self._key = key

    def fetch(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
              window: MacroFetchWindow) -> ProviderFetchResult:
        result = super().fetch(series, source, window)
        raw = sanitize_macro_value(result.raw_payload, self._key)
        assert isinstance(raw, dict)
        return replace(result, raw_payload=raw)


@contextmanager
def open_fred_macro_sync_runtime(settings: Settings, *, transport: httpx.BaseTransport | None = None
                                ) -> Iterator[MacroSyncRuntime]:
    if not settings.database_url:
        raise ValueError('FRED_MACRO_DATABASE_REQUIRED')
    if not settings.fred_api_key:
        raise ValueError('FRED_MACRO_KEY_REQUIRED')
    catalog = load_active_catalog()
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        client = resources.enter_context(httpx.Client(transport=transport,
            timeout=catalog.defaults.request_timeout_seconds, follow_redirects=False))
        http = resources.enter_context(MacroHttpTransport(client=client, max_attempts=1,
            backoff_seconds=catalog.defaults.retry_backoff_seconds))
        bounded = _LimitedTransport(http, settings.macro_fred_request_limit)

        def verify(options: MacroSyncOptions) -> None:
            # 共享发布锁内，拒绝时没有 HTTP 或新采集 JobRun。
            if (options.series_ids != SERIES_IDS or options.publish or options.mode != 'live'
                    or not options.force or options.history_years != settings.macro_history_years):
                raise ValueError('FRED_MACRO_CONFIG_INVALID')
            end = options.as_of.astimezone(ZoneInfo(settings.sync_timezone)).date()
            states = {s.series_id:s for s in MacroSeriesStateRepository(factory,catalog.catalog_version).get_all()}
            observations = MacroObservationRepository(factory)
            for sid, (category, frequency, unit, precision, freshness, fred_id, transform, bls_id) in zip(SERIES_IDS, _CONTRACTS):
                definition = catalog.require(sid)
                if ((definition.region, definition.category, definition.frequency, definition.unit,
                        definition.seasonal_adjustment, definition.display_precision, definition.freshness_days)
                        != ('US', category, frequency, unit, 'seasonally_adjusted', precision, freshness)
                        or definition.sources != _approved_sources(fred_id, transform, bls_id)):
                    raise ValueError('FRED_MACRO_SOURCE_INVALID')
                if state_is_future(states.get(sid), options.as_of, end):
                    raise ValueError('FRED_MACRO_CLOCK_INVALID')
                if observations.latest_visible(definition, options.as_of, end, provider='fred') is None:
                    raise ValueError('FRED_MACRO_CORE_REQUIRED')
            bounded.reset()

        key = settings.fred_api_key.get_secret_value()
        providers = (CalendarFredMacroProvider(key, transport=bounded), _SafeBlsProvider(bounded, key))
        history = SqlMacroSyncHistory(factory, job_name='sync-fred-macro')
        job = _build_macro_job(factory, MacroProviderRegistry(providers), engine, catalog,
            settings.sync_timezone, history=history, before_work=verify, settings=settings)
        yield MacroSyncRuntime(job, catalog)
