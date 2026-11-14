from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from app.cli import main as main_module
from app.config import Settings
from app.connectors.macro.contracts import ProviderFetchResult
from app.macro.jobs.macro_sync import MacroSyncReport


AS_OF = datetime(2026, 9, 20, tzinfo=UTC)


def _report(status: str) -> MacroSyncReport:
    return MacroSyncReport(status, "run-1", "2026-W38:v1", 1, AS_OF, "v1")


@pytest.mark.parametrize("status,exit_code", [
    ("completed", 0), ("skipped", 0), ("partial", 4), ("failed", 4), ("locked", 5),
])
def test_sync_macro_emits_one_final_json_and_maps_status(monkeypatch, status, exit_code) -> None:
    seen = []

    class Job:
        def run(self, options):
            seen.append(options)
            return _report(status)

    @contextmanager
    def runtime(settings):
        yield type("Runtime", (), {"job": Job(), "catalog": type("Catalog", (), {
            "catalog_version": "v1", "catalog_hash": "hash"
        })()})()

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(database_url="sqlite://"))
    monkeypatch.setattr(main_module, "open_macro_sync_runtime", runtime, raising=False)
    result = CliRunner().invoke(main_module.app, ["sync-macro", "--as-of", "2026-09-20", "--force"])
    assert result.exit_code == exit_code, result.output
    assert len(result.stdout.strip().splitlines()) == 1
    assert json.loads(result.stdout)["status"] == status
    assert seen[0].mode == "replay"
    assert seen[0].as_of.isoformat() == "2026-09-20T23:59:59.999999+08:00"
    assert seen[0].force is True
    assert seen[0].trigger == "cli"
    assert "preflight" in result.stderr


def test_sync_macro_without_as_of_uses_live_mode(monkeypatch) -> None:
    seen = []

    class Job:
        def run(self, options):
            seen.append(options)
            return _report("completed")

    @contextmanager
    def runtime(settings):
        yield type("Runtime", (), {"job": Job(), "catalog": type("Catalog", (), {
            "catalog_version": "v1", "catalog_hash": "hash"
        })()})()

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(database_url="sqlite://"))
    monkeypatch.setattr(main_module, "open_macro_sync_runtime", runtime, raising=False)
    result = CliRunner().invoke(main_module.app, ["sync-macro"])
    assert result.exit_code == 0, result.output
    assert seen[0].mode == "live"
    assert seen[0].as_of.utcoffset() is not None


def test_sync_macro_invalid_date_exits_two() -> None:
    result = CliRunner().invoke(main_module.app, ["sync-macro", "--as-of", "2026-02-30"])
    assert result.exit_code == 2


def test_sync_macro_missing_database_exits_three_without_secret(monkeypatch) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(fred_api_key="fred-secret"))
    result = CliRunner().invoke(main_module.app, ["sync-macro"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["status"] == "failed"
    assert "fred-secret" not in result.output


def test_sync_macro_redacts_setup_failure(monkeypatch) -> None:
    @contextmanager
    def runtime(settings):
        raise RuntimeError("postgresql://user:db-secret@host/db fred-secret tushare-secret")
        yield

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(
        database_url="postgresql://user:db-secret@host/db",
        fred_api_key="fred-secret", tushare_token="tushare-secret",
    ))
    monkeypatch.setattr(main_module, "open_macro_sync_runtime", runtime, raising=False)
    result = CliRunner().invoke(main_module.app, ["sync-macro"])
    assert result.exit_code == 3
    assert all(secret not in result.output for secret in ("db-secret", "fred-secret", "tushare-secret"))


def test_sync_macro_redacts_database_password_even_without_url(monkeypatch) -> None:
    @contextmanager
    def runtime(settings):
        raise RuntimeError("authentication failed for db-secret")
        yield

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(
        database_url="postgresql://user:db-secret@host/db",
    ))
    monkeypatch.setattr(main_module, "open_macro_sync_runtime", runtime)
    result = CliRunner().invoke(main_module.app, ["sync-macro"])
    assert result.exit_code == 3
    assert "db-secret" not in result.output


def test_smoke_macro_rejects_unknown_provider() -> None:
    result = CliRunner().invoke(main_module.app, ["smoke-macro", "--provider", "unknown"])
    assert result.exit_code == 2


def test_smoke_macro_missing_credential_exits_three_without_database(monkeypatch) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings())
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: pytest.fail("database constructed"))
    result = CliRunner().invoke(main_module.app, ["smoke-macro", "--provider", "fred"])
    assert result.exit_code == 3


def test_smoke_macro_settings_failure_exits_three(monkeypatch) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: (_ for _ in ()).throw(ValueError("settings invalid")))
    result = CliRunner().invoke(main_module.app, ["smoke-macro", "--provider", "fred"])
    assert result.exit_code == 3


@pytest.mark.parametrize("http_status,exit_code", [(400, 4), (401, 3), (403, 3)])
def test_smoke_macro_provider_failure_exits_by_error_without_database(monkeypatch, http_status, exit_code) -> None:
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(fred_api_key="fred-secret"))
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: pytest.fail("database constructed"))
    real_client = httpx.Client
    monkeypatch.setattr(main_module.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(http_status)), **{
            key: value for key, value in kwargs.items() if key != "transport"
        }
    ))
    result = CliRunner().invoke(main_module.app, ["smoke-macro", "--provider", "fred"])
    assert result.exit_code == exit_code, result.output
    payload = json.loads(result.stdout)
    assert payload["provider"] == "fred"
    assert payload["status"] == "failed"
    assert payload["count"] == 0
    assert "fred-secret" not in result.output


def test_smoke_macro_tushare_requests_one_seven_day_daily_series(monkeypatch) -> None:
    windows = []

    class Client:
        def __init__(self, token):
            assert token == "test-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Provider:
        def __init__(self, client):
            pass

        def fetch(self, series, source, window):
            windows.append((series.id, (window.end - window.start).days))
            return ProviderFetchResult("tushare", "no_change", (), {}, 1)

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: Settings(tushare_token="test-token"))
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda url: pytest.fail("database constructed"))
    monkeypatch.setattr(main_module, "TushareClient", Client)
    monkeypatch.setattr(main_module, "TushareMacroProvider", Provider)
    result = CliRunner().invoke(main_module.app, ["smoke-macro", "--provider", "tushare"])
    assert result.exit_code == 0, result.output
    assert windows == [("cn.rate.dr007", 6)]
    assert json.loads(result.stdout)["series"] == "cn.rate.dr007"
