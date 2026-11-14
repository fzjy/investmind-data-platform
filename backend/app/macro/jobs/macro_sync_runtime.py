from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.connectors.macro.bea import BeaMacroProvider
from app.connectors.macro.bls import BlsMacroProvider
from app.connectors.macro.chinamoney import ChinaMoneyMacroProvider
from app.connectors.macro.contracts import MacroProvider, ProviderAuthError, UnavailableMacroProvider
from app.connectors.macro.fred import CalendarFredMacroProvider
from app.connectors.macro.nbs import NbsMacroProvider
from app.connectors.macro.nyfed import NyFedMacroProvider
from app.connectors.macro.pboc import PbocMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.connectors.macro.transport import MacroHttpTransport
from app.connectors.macro.tushare import TushareMacroProvider
from app.connectors.macro.us_treasury import UsTreasuryMacroProvider
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync import MacroSyncJob
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher


class MacroSyncRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MacroSyncRuntime:
    job: MacroSyncJob
    catalog: MacroSeriesCatalog


def _required_database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise MacroSyncRuntimeError("sync-macro requires DANG_DATABASE_URL")
    return settings.database_url


def _build_macro_providers(
    settings: Settings, http: MacroHttpTransport, resources: ExitStack,
    catalog: MacroSeriesCatalog,
) -> tuple[MacroProvider, ...]:
    selected = {source.provider for series in catalog.series for source in series.sources}
    providers: list[MacroProvider] = []
    public: dict[str, Callable[[MacroHttpTransport], MacroProvider]] = {
        "nbs": NbsMacroProvider,
        "pboc": PbocMacroProvider,
        "chinamoney": ChinaMoneyMacroProvider,
        "bls": BlsMacroProvider,
        "bea": BeaMacroProvider,
        "nyfed": NyFedMacroProvider,
        "us_treasury": UsTreasuryMacroProvider,
    }
    for provider_id in sorted(selected):
        if provider_id == "fred":
            key = settings.fred_api_key.get_secret_value() if settings.fred_api_key else ""
            providers.append(
                CalendarFredMacroProvider(key, transport=http) if key else
                UnavailableMacroProvider("fred", ProviderAuthError("FRED_KEY_MISSING"))
            )
        elif provider_id == "tushare":
            token = settings.tushare_token.get_secret_value() if settings.tushare_token else ""
            if token:
                rate = (
                    settings.tushare_vip_requests_per_minute
                    if settings.tushare_mode == "vip" else settings.tushare_standard_requests_per_minute
                )
                client = TushareClient(token, rate_limiter=RequestRateLimiter(rate))
                resources.callback(client.close)
                providers.append(TushareMacroProvider(client))
            else:
                providers.append(UnavailableMacroProvider(
                    "tushare", ProviderAuthError("TUSHARE_TOKEN_MISSING")
                ))
        elif provider_id in public:
            providers.append(public[provider_id](http))
        else:
            raise MacroSyncRuntimeError("macro catalog has no constructible provider")
    if not any(not isinstance(item, UnavailableMacroProvider) for item in providers):
        raise MacroSyncRuntimeError("no macro catalog source is constructible")
    return tuple(providers)


def _build_macro_job(
    factory: sessionmaker[Session], registry: MacroProviderRegistry,
    engine: Engine, catalog: MacroSeriesCatalog, timezone: str,
    *, history: SqlMacroSyncHistory | None = None,
    before_work: Callable[[MacroSyncOptions], None] | None = None,
    settings: Settings | None = None,
) -> MacroSyncJob:
    history = history or SqlMacroSyncHistory(factory)
    states = MacroSeriesStateRepository(factory, catalog.catalog_version)
    from app.macro.enterprise_contract import enterprise_loader
    from app.macro.extensions.view import load_extension_inputs
    publisher = MacroPublishJob(
        MacroMartBuilder(
            MacroRepository(factory), catalog=catalog, state_repository=states,
            enterprise_loader=enterprise_loader(factory, settings) if settings is not None else None,
            extension_loader=(lambda cutoff: load_extension_inputs(factory, cutoff)) if settings is not None else None,
        ),
        SnapshotPublisher(factory), history,
    )
    return MacroSyncJob(
        catalog=catalog, registry=registry,
        observation_repository=MacroObservationRepository(factory),
        state_repository=states, history=history,
        lock=job_lock_for_engine(engine), publisher=publisher,
        timezone=timezone,
        before_work=before_work,
    )


@contextmanager
def open_macro_sync_runtime(
    settings: Settings, *, transport: httpx.BaseTransport | None = None,
) -> Iterator[MacroSyncRuntime]:
    database_url = _required_database_url(settings)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with ExitStack() as resources:
        try:
            engine, factory = create_engine_and_session(database_url)
        except Exception:
            raise MacroSyncRuntimeError("macro database connection failed") from None
        resources.callback(engine.dispose)
        try:
            client = httpx.Client(
                transport=transport,
                timeout=catalog.defaults.request_timeout_seconds,
            )
            resources.callback(client.close)
            http = MacroHttpTransport(
                client=client,
                max_attempts=catalog.defaults.max_attempts,
                backoff_seconds=catalog.defaults.retry_backoff_seconds,
            )
            resources.callback(http.close)
            providers = _build_macro_providers(settings, http, resources, catalog)
            registry = MacroProviderRegistry(providers)
            job = _build_macro_job(factory, registry, engine, catalog, settings.sync_timezone, settings=settings)
        except MacroSyncRuntimeError:
            raise
        except Exception:
            raise MacroSyncRuntimeError("macro provider construction failed") from None
        yield MacroSyncRuntime(job, catalog)
