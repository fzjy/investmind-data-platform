from __future__ import annotations

from pathlib import Path

import pytest

import app.stocks.jobs.daily_sync_runtime as runtime_module
from app.config import Settings
from app.connectors.tushare.strategies import TushareMode
from app.stocks.jobs.daily_sync_runtime import (
    DailySyncRuntime,
    DailySyncRuntimeError,
    open_daily_sync_runtime,
    redact_runtime_error,
)
from app.stocks.universes.models import UniverseSelection


SELECTION = UniverseSelection(
    profile="representative-50",
    version="2026-09-04-v1",
    coverage="representative",
    symbols=("600519.SH",),
    fingerprint="representative-fingerprint",
)


def make_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "sqlite+pysqlite:///:memory:",
        "tushare_token": "test-token",
        "tushare_mode": "standard",
        "tushare_standard_requests_per_minute": 17,
        "tushare_vip_requests_per_minute": 71,
        "tushare_page_size": 123,
    }
    values.update(changes)
    return Settings(**values)


def monkeypatch_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    client_enter_error: Exception | None = None,
    pipeline_error: Exception | None = None,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeEngine:
        def dispose(self) -> None:
            events.append("engine.dispose")

    class FakeRepository:
        def __init__(self, factory: object) -> None:
            captured["repository_factory"] = factory

        def active_symbols(self) -> tuple[str, ...]:
            return SELECTION.symbols

        def security_states(self, symbols: tuple[str, ...]) -> dict[str, object]:
            captured["security_state_symbols"] = symbols
            return {}

    class FakeResolver:
        def __init__(self, **dependencies: object) -> None:
            captured["resolver_dependencies"] = dependencies

        def resolve(
            self,
            profile: str,
            *,
            universe_file: Path | None,
        ) -> UniverseSelection:
            captured["resolve"] = (profile, universe_file)
            return SELECTION

    class FakeClient:
        def __init__(self, token: str, **kwargs: object) -> None:
            captured["client"] = (token, kwargs)

        def __enter__(self) -> "FakeClient":
            events.append("client.enter")
            if client_enter_error is not None:
                raise client_enter_error
            return self

        def close(self) -> None:
            events.append("client.close")

        def __exit__(self, *_args: object) -> None:
            self.close()

    class FakeRowFilter:
        def __init__(self, selection: UniverseSelection) -> None:
            captured["row_filter_selection"] = selection

    class FakePipeline:
        def __init__(
            self,
            client: object,
            factory: object,
            *,
            row_filter: object,
        ) -> None:
            captured["pipeline"] = (client, factory, row_filter)
            if pipeline_error is not None:
                raise pipeline_error

        def run(self, plan: object, *, mode: str) -> None:
            raise AssertionError("runtime assembly must not call Tushare")

    class FakePageRunner:
        def __init__(self, run_page: object, cursor: object, **kwargs: object) -> None:
            captured["page_runner"] = (run_page, cursor, kwargs)

    class FakeDailySyncJob:
        def __init__(self, **kwargs: object) -> None:
            captured["job_kwargs"] = kwargs

    engine = FakeEngine()
    factory = object()
    monkeypatch.setattr(
        runtime_module,
        "create_engine_and_session",
        lambda database_url: (engine, factory),
    )
    monkeypatch.setattr(runtime_module, "DailySyncRepository", FakeRepository)
    monkeypatch.setattr(runtime_module, "UniverseResolver", FakeResolver)
    monkeypatch.setattr(runtime_module, "TushareClient", FakeClient)
    monkeypatch.setattr(runtime_module, "UniverseRowFilter", FakeRowFilter)
    monkeypatch.setattr(runtime_module, "IngestionPipeline", FakePipeline)
    monkeypatch.setattr(runtime_module, "PaginatedPlanRunner", FakePageRunner)
    monkeypatch.setattr(runtime_module, "DailySyncJob", FakeDailySyncJob)
    monkeypatch.setattr(
        runtime_module,
        "RequestRateLimiter",
        lambda requests_per_minute: ("limiter", requests_per_minute),
    )
    monkeypatch.setattr(
        runtime_module,
        "SqlJobCursorRepository",
        lambda cursor_factory, **kwargs: (cursor_factory, kwargs),
    )
    monkeypatch.setattr(
        runtime_module,
        "BuildAndPublishJob",
        lambda **kwargs: ("build-job", kwargs),
    )
    monkeypatch.setattr(
        runtime_module,
        "job_lock_for_engine",
        lambda value: ("lock", value),
    )
    for dependency_name in (
        "CoreSnapshotRepository",
        "DerivedPipeline",
        "DefaultMartBuilders",
        "QualityGate",
        "SnapshotPublisher",
        "SqlBuildHistory",
        "SqlDailySyncHistory",
    ):
        monkeypatch.setattr(
            runtime_module,
            dependency_name,
            lambda *args, _name=dependency_name, **kwargs: (_name, args, kwargs),
        )
    return {"captured": captured, "engine": engine, "factory": factory}


def test_open_runtime_builds_one_selection_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    state = monkeypatch_runtime_dependencies(monkeypatch, events)

    with open_daily_sync_runtime(
        make_settings(),
        "representative-50",
        None,
    ) as runtime:
        captured = state["captured"]
        assert isinstance(runtime, DailySyncRuntime)
        assert runtime.selection is SELECTION
        assert runtime.mode is TushareMode.STANDARD
        assert runtime.session_factory is state["factory"]
        assert runtime.job is not None
        assert captured["resolve"] == ("representative-50", None)
        assert captured["row_filter_selection"] is runtime.selection
        assert captured["job_kwargs"]["universe"] is runtime.selection
        assert captured["page_runner"][2] == {
            "scope_kind": "routine",
            "status_factory": state["factory"],
            "mode": TushareMode.STANDARD,
            "page_size": 123,
            "fingerprint_context": SELECTION.fingerprint,
        }
        assert captured["client"] == (
            "test-token",
            {"rate_limiter": ("limiter", 17)},
        )

    assert events[-2:] == ["client.close", "engine.dispose"]
    assert events.count("client.close") == 1


def test_open_runtime_closes_client_when_client_enter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    error = RuntimeError("client enter failed")
    monkeypatch_runtime_dependencies(
        monkeypatch,
        events,
        client_enter_error=error,
    )

    with pytest.raises(RuntimeError, match="client enter failed") as raised:
        with open_daily_sync_runtime(
            make_settings(),
            "representative-50",
            None,
        ):
            raise AssertionError("runtime must not yield after enter failure")

    assert raised.value is error
    assert events == ["client.enter", "client.close", "engine.dispose"]


def test_open_runtime_closes_resources_when_assembly_raises_after_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    error = RuntimeError("pipeline assembly failed")
    monkeypatch_runtime_dependencies(
        monkeypatch,
        events,
        pipeline_error=error,
    )

    with pytest.raises(RuntimeError, match="pipeline assembly failed") as raised:
        with open_daily_sync_runtime(
            make_settings(),
            "representative-50",
            None,
        ):
            raise AssertionError("runtime must not yield after assembly failure")

    assert raised.value is error
    assert events == ["client.enter", "client.close", "engine.dispose"]


def test_open_runtime_closes_resources_when_consumer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch_runtime_dependencies(monkeypatch, events)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with open_daily_sync_runtime(
            make_settings(tushare_mode="vip"),
            "representative-50",
            None,
        ) as runtime:
            assert runtime.mode is TushareMode.VIP
            raise RuntimeError("consumer failed")

    assert events[-2:] == ["client.close", "engine.dispose"]
    assert events.count("client.close") == 1


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (make_settings(database_url=None), "sync-daily requires DANG_DATABASE_URL"),
        (make_settings(tushare_token=None), "sync-daily requires DANG_TUSHARE_TOKEN"),
    ],
)
def test_open_runtime_requires_database_and_token(
    settings: Settings,
    message: str,
) -> None:
    with pytest.raises(DailySyncRuntimeError, match=message):
        with open_daily_sync_runtime(settings, "representative-50", None):
            raise AssertionError("invalid runtime must not open")


def test_redact_runtime_error_reuses_cli_sensitive_value_rules() -> None:
    assert redact_runtime_error(
        "test-token sqlite:///secret.db postgresql://user:password@db/dang",
        "test-token",
        "sqlite:///secret.db",
    ) == "*** *** postgresql://user:***@db/dang"
