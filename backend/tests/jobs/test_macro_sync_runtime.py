from __future__ import annotations

import httpx
import pytest
from sqlalchemy import event

from app.config import Settings
from app.connectors.macro.contracts import UnavailableMacroProvider
from app.connectors.macro.registry import MacroProviderRegistry
from app.jobs.locks import PostgresAdvisoryJobLock
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


def test_fred_only_credentials_leave_tushare_unavailable(pg_schema_url) -> None:
    from app.macro.jobs import macro_sync_runtime as runtime_module

    settings = Settings(database_url=pg_schema_url, fred_api_key="fred-secret")
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    with runtime_module.open_macro_sync_runtime(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    )) as runtime:
        assert runtime.catalog.catalog_hash == catalog.catalog_hash
        assert isinstance(runtime.job._registry.require("tushare"), UnavailableMacroProvider)
        assert runtime.job._registry.require("fred").provider_id == "fred"


def test_missing_fred_key_registers_unavailable_provider(pg_schema_url) -> None:
    from app.macro.jobs.macro_sync_runtime import open_macro_sync_runtime

    with open_macro_sync_runtime(Settings(database_url=pg_schema_url), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    )) as runtime:
        assert isinstance(runtime.job._registry.require("fred"), UnavailableMacroProvider)
        assert runtime.job._registry.require("bls").provider_id == "bls"


def test_runtime_closes_engine_and_transport_after_success(pg_schema_url, monkeypatch) -> None:
    from app.macro.jobs import macro_sync_runtime as runtime_module

    disposed = []
    closed = []
    real_create = runtime_module.create_engine_and_session

    def create(url):
        engine, factory = real_create(url)
        real_dispose = engine.dispose

        def dispose():
            disposed.append(True)
            real_dispose()

        monkeypatch.setattr(engine, "dispose", dispose)
        return engine, factory

    monkeypatch.setattr(runtime_module, "create_engine_and_session", create)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    real_close = transport.close

    def close():
        closed.append(True)
        real_close()

    monkeypatch.setattr(transport, "close", close)
    with runtime_module.open_macro_sync_runtime(Settings(database_url=pg_schema_url), transport=transport):
        pass
    assert disposed == [True]
    assert closed == [True]


def test_provider_construction_failure_disposes_engine(pg_schema_url, monkeypatch) -> None:
    from app.macro.jobs import macro_sync_runtime as runtime_module

    disposed = []
    real_create = runtime_module.create_engine_and_session

    def create(url):
        engine, factory = real_create(url)
        event.listen(engine, "engine_disposed", lambda _: disposed.append(True))
        return engine, factory

    monkeypatch.setattr(runtime_module, "create_engine_and_session", create)
    monkeypatch.setattr(runtime_module, "BlsMacroProvider", lambda transport: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(runtime_module.MacroSyncRuntimeError, match="provider construction failed"):
        with runtime_module.open_macro_sync_runtime(Settings(database_url=pg_schema_url), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )):
            pass
    assert disposed == [True]


def test_postgres_engine_selects_advisory_lock(pg_schema_url) -> None:
    from app.macro.jobs import macro_sync_runtime as runtime_module

    with runtime_module.open_macro_sync_runtime(Settings(database_url=pg_schema_url), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    )) as runtime:
        assert isinstance(runtime.job._lock, PostgresAdvisoryJobLock)


def test_missing_database_error_does_not_echo_secret() -> None:
    from app.macro.jobs.macro_sync_runtime import MacroSyncRuntimeError, open_macro_sync_runtime

    with pytest.raises(MacroSyncRuntimeError) as error:
        with open_macro_sync_runtime(Settings(database_url=None, database_read_url=None, fred_api_key="fred-secret", tushare_token="tushare-secret")):
            pass
    assert "fred-secret" not in str(error.value)
    assert "tushare-secret" not in str(error.value)


def test_engine_creation_error_does_not_echo_database_password(monkeypatch) -> None:
    from app.macro.jobs import macro_sync_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "create_engine_and_session", lambda url: (_ for _ in ()).throw(
        RuntimeError("connection failed with db-secret")
    ))
    with pytest.raises(runtime_module.MacroSyncRuntimeError) as error:
        with runtime_module.open_macro_sync_runtime(Settings(
            database_url="postgresql://user:db-secret@host/db",
        )):
            pass
    assert "db-secret" not in str(error.value)
