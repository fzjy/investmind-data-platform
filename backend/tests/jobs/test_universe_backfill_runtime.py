from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.stocks.jobs.universe_backfill_runtime as runtime_module
from app.config import Settings
from app.connectors.tushare.strategies import TushareMode
from app.db.base import Base
from app.db.session import create_engine_and_session
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.ingestion.pagination import PaginatedPlanRunner
from app.ingestion.pipeline import IngestionPipeline
from app.stocks.jobs.backfill import BackfillJob
from app.jobs.build_and_publish import BuildAndPublishJob
from app.jobs.cursors import SqlJobCursorRepository
from app.stocks.jobs.universe_backfill_history import SqlUniverseBackfillHistory
from app.stocks.jobs.universe_backfill_runtime import (
    UniverseBackfillRuntime,
    UniverseBackfillRuntimeError,
    open_universe_backfill_runtime,
)
from app.stocks.universes.models import UniverseSelection


SELECTION = UniverseSelection(
    profile="representative-50",
    version="2026-09-04-v1",
    coverage="representative",
    symbols=("600519.SH",),
    fingerprint="selection-fingerprint",
)


def database_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "sqlite+pysqlite:///:memory:",
        "tushare_token": "test-token",
        "tushare_mode": "standard",
        "tushare_standard_requests_per_minute": 17,
        "tushare_vip_requests_per_minute": 71,
        "tushare_page_size": 123,
        "sync_universe": "representative-50",
        "backfill_as_of": date(2026, 9, 12),
        "backfill_market_years": 3,
        "backfill_batch_size": 10,
    }
    values.update(changes)
    return Settings(**values)


class RecordingClient:
    instances: list[RecordingClient] = []

    def __init__(self, token: str, **kwargs: object) -> None:
        self.token = token
        self.kwargs = kwargs
        self.close_calls = 0
        self.instances.append(self)

    def __enter__(self) -> RecordingClient:
        return self

    def close(self) -> None:
        self.close_calls += 1

    def call(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("runtime assembly must not call Tushare")


def test_runtime_uses_settings_selection_and_shared_filtered_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.sqlite3'}"
    engine, _factory = create_engine_and_session(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    RecordingClient.instances.clear()
    monkeypatch.setattr(runtime_module, "TushareClient", RecordingClient)

    with open_universe_backfill_runtime(
        database_settings(database_url=database_url),
    ) as runtime:
        assert isinstance(runtime, UniverseBackfillRuntime)
        assert runtime.selection.profile == "representative-50"
        assert runtime.options.selection is runtime.selection
        assert runtime.options.mode is TushareMode.STANDARD
        assert runtime.options.market_years == 3
        assert runtime.options.batch_size == 10
        assert runtime.mode is TushareMode.STANDARD

        batch_runner = runtime.job._batch_runner_factory(
            "backfill-universe:run-1"
        )
        assert isinstance(batch_runner, BackfillJob)
        assert batch_runner._history is None
        assert isinstance(batch_runner._runner, PaginatedPlanRunner)
        assert (
            batch_runner._runner._fingerprint_context
            == runtime.selection.fingerprint
        )
        assert isinstance(
            batch_runner._runner._cursor_store,
            SqlJobCursorRepository,
        )
        assert (
            batch_runner._runner._cursor_store._job_name
            == "backfill-universe:run-1"
        )
        run_page_closure = tuple(
            cell.cell_contents
            for cell in batch_runner._runner._run_page.__closure__ or ()
        )
        pipeline = next(
            value
            for value in run_page_closure
            if isinstance(value, IngestionPipeline)
        )
        assert pipeline._row_filter._selection is runtime.selection
        assert isinstance(runtime.job._history, SqlUniverseBackfillHistory)
        assert isinstance(runtime.job._publisher, BuildAndPublishJob)
        assert isinstance(
            runtime.job._publisher._repository,
            CoreSnapshotRepository,
        )

    assert len(RecordingClient.instances) == 1
    assert RecordingClient.instances[0].token == "test-token"
    assert RecordingClient.instances[0].close_calls == 1


def patch_lifecycle_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    client_enter_error: BaseException | None = None,
) -> None:
    class FakeEngine:
        dialect = SimpleNamespace(name="sqlite")

        def dispose(self) -> None:
            events.append("engine.dispose")

    class FakeRepository:
        def __init__(self, _factory: object) -> None:
            pass

        def active_symbols(self) -> tuple[str, ...]:
            return SELECTION.symbols

        def security_states(self, _symbols: tuple[str, ...]) -> dict[str, object]:
            return {}

    class FakeResolver:
        def __init__(self, **_dependencies: object) -> None:
            pass

        def resolve(
            self,
            _profile: str,
            *,
            universe_file: Path | None,
        ) -> UniverseSelection:
            assert universe_file is None
            return SELECTION

    class FakeClient:
        def __init__(self, _token: str, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            events.append("client.enter")
            if client_enter_error is not None:
                raise client_enter_error
            return self

        def close(self) -> None:
            events.append("client.close")

    monkeypatch.setattr(
        runtime_module,
        "create_engine_and_session",
        lambda _database_url: (FakeEngine(), lambda: None),
    )
    monkeypatch.setattr(runtime_module, "DailySyncRepository", FakeRepository)
    monkeypatch.setattr(runtime_module, "UniverseResolver", FakeResolver)
    monkeypatch.setattr(runtime_module, "TushareClient", FakeClient)


def assert_resources_closed_once(events: list[str]) -> None:
    assert events[-2:] == ["client.close", "engine.dispose"]
    assert events.count("client.close") == 1
    assert events.count("engine.dispose") == 1


def test_runtime_closes_client_and_engine_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    patch_lifecycle_dependencies(monkeypatch, events)

    with open_universe_backfill_runtime(database_settings()):
        events.append("body")

    assert events == [
        "client.enter",
        "body",
        "client.close",
        "engine.dispose",
    ]
    assert_resources_closed_once(events)


def test_runtime_closes_client_and_engine_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    patch_lifecycle_dependencies(monkeypatch, events)

    def raising_filter(_selection: UniverseSelection) -> None:
        raise RuntimeError("controlled construction failure")

    monkeypatch.setattr(runtime_module, "UniverseRowFilter", raising_filter)

    with pytest.raises(RuntimeError, match="controlled construction failure"):
        with open_universe_backfill_runtime(database_settings()):
            raise AssertionError("failed construction must not yield")

    assert_resources_closed_once(events)


def test_runtime_closes_client_and_engine_when_client_enter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    error = RuntimeError("client enter failed")
    patch_lifecycle_dependencies(
        monkeypatch,
        events,
        client_enter_error=error,
    )

    with pytest.raises(RuntimeError, match="client enter failed") as raised:
        with open_universe_backfill_runtime(database_settings()):
            raise AssertionError("failed client enter must not yield")

    assert raised.value is error
    assert events == ["client.enter", "client.close", "engine.dispose"]
    assert_resources_closed_once(events)


@pytest.mark.parametrize("error_type", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_runtime_closes_resources_and_propagates_body_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    events: list[str] = []
    patch_lifecycle_dependencies(monkeypatch, events)
    error = error_type("consumer failed")

    with pytest.raises(error_type) as raised:
        with open_universe_backfill_runtime(database_settings()):
            raise error

    assert raised.value is error
    assert_resources_closed_once(events)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"database_url": None},
            "backfill-universe requires DANG_DATABASE_URL",
        ),
        (
            {"tushare_token": None},
            "backfill-universe requires DANG_TUSHARE_TOKEN",
        ),
        (
            {"backfill_as_of": None},
            "backfill-universe requires DANG_BACKFILL_AS_OF",
        ),
    ],
)
def test_runtime_requires_database_token_and_as_of(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(UniverseBackfillRuntimeError, match=message):
        with open_universe_backfill_runtime(database_settings(**changes)):
            raise AssertionError("invalid runtime must not open")
