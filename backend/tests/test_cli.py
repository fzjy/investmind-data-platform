from __future__ import annotations

import json


def _business_stderr(result) -> str:
    """既有stderr业务诊断仍保持原样；新增结构化事件在独立集成测试验证。"""
    kept = []
    for line in result.stderr.splitlines(keepends=True):
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if not isinstance(item, dict) or item.get("event") not in {"cli_command", "safety_alert"}:
            kept.append(line)
    return "".join(kept)
import traceback
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from sqlalchemy import MetaData, Table, inspect as sqlalchemy_inspect, select
from typer.testing import CliRunner

import app.cli.main as main_module
import app.config as config_module
from app.cli.main import app
from app.config import Settings
from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.errors import TushareAuthError, TushareTransientError
from app.connectors.tushare.strategies import Dataset, SyncScope, TushareMode, build_request_plans
from app.db.base import Base, ExternalBase, MartBase
from app.db.models import (
    AdjustmentFactor,
    DailyPrice,
    IngestionBatch,
    JobCursor,
    JobFailure,
    JobRun,
    Security,
    TradeCalendar,
)
from app.db.session import create_engine_and_session
from app.ingestion.pagination import PaginatedResult
from app.ingestion.pipeline import BatchResult
from app.stocks.jobs.backfill import PlanFailure
from app.jobs.failure_retries import serialize_replay_request
from app.stocks.jobs.daily_sync import DailySyncReport
from app.stocks.jobs.daily_sync_runtime import DailySyncRuntime
from app.jobs.sync_status import SyncStatusReport
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillOptions,
    UniverseBackfillPlanningError,
)
from app.stocks.jobs.universe_backfill_runtime import UniverseBackfillRuntimeError
from app.stocks.universes import UniverseConfigError
from app.stocks.universes.models import UniverseSelection


runner = CliRunner()


class _FakeUniverseBackfillJob:
    def __init__(
        self,
        captured: dict[str, object],
        status: str,
        *,
        plan_error: BaseException | None = None,
        run_error: BaseException | None = None,
    ) -> None:
        self._captured = captured
        self._status = status
        self._plan_error = plan_error
        self._run_error = run_error

    def plan(self, options: UniverseBackfillOptions) -> SimpleNamespace:
        self._captured["planned_options"] = options
        if self._plan_error is not None:
            raise self._plan_error
        return SimpleNamespace(
            preflight_payload=lambda: {
                "event": "backfill-universe.preflight",
                "asOf": "2026-09-12",
                "universeSize": 50,
            }
        )

    def run(self, options: UniverseBackfillOptions) -> SimpleNamespace:
        self._captured["run_options"] = options
        if self._run_error is not None:
            raise self._run_error
        return SimpleNamespace(
            status=self._status,
            to_payload=lambda: {
                "status": self._status,
                "jobRunId": "universe-run-1",
            },
        )


def _universe_backfill_settings(
    *,
    profile: str = "representative-50",
    as_of: date | None = date(2026, 9, 12),
    database_url: str = "sqlite+pysqlite:///:memory:",
    token: str = "test-token",
) -> Settings:
    return Settings(
        _env_file=None,
        data_backend="database",
        database_url=database_url,
        tushare_token=token,
        sync_universe=profile,
        backfill_as_of=as_of,
        backfill_market_years=3,
        backfill_batch_size=10,
    )


def _fake_universe_backfill_runtime(
    captured: dict[str, object],
    *,
    status: str = "completed",
    open_error: BaseException | None = None,
    plan_error: BaseException | None = None,
    run_error: BaseException | None = None,
):
    @contextmanager
    def open_runtime(settings: Settings):
        captured["settings"] = settings
        if open_error is not None:
            raise open_error
        selection = UniverseSelection(
            profile=settings.sync_universe,
            version="2026-09-12-v1",
            coverage=(
                "full-market"
                if settings.sync_universe == "all"
                else "representative"
            ),
            symbols=tuple(f"{index:06d}.SZ" for index in range(50)),
            fingerprint="selection-fingerprint",
        )
        options = UniverseBackfillOptions(
            as_of=settings.backfill_as_of or date(2026, 9, 12),
            mode=TushareMode(settings.tushare_mode),
            selection=selection,
            market_years=settings.backfill_market_years,
            financial_start=settings.backfill_financial_start,
            batch_size=settings.backfill_batch_size,
        )
        yield SimpleNamespace(
            job=_FakeUniverseBackfillJob(
                captured,
                status,
                plan_error=plan_error,
                run_error=run_error,
            ),
            options=options,
        )

    return open_runtime


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return tuple(chain)


def test_backfill_universe_reads_business_options_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(captured),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 0
    assert captured["settings"] is settings
    assert captured["planned_options"].force is False
    assert captured["run_options"].force is False
    assert json.loads(result.stdout)["status"] == "completed"


@pytest.mark.parametrize(
    "business_option",
    [
        ["--as-of", "2026-09-12"],
        ["--market-years", "3"],
        ["--batch-size", "10"],
        ["--mode", "standard"],
    ],
)
def test_backfill_universe_rejects_business_options(
    business_option: list[str],
) -> None:
    result = runner.invoke(app, ["backfill-universe", *business_option])

    assert result.exit_code == 2
    assert "--help" in result.stderr
    assert business_option[0] not in result.stderr


def test_backfill_universe_help_exposes_only_two_action_flags() -> None:
    result = runner.invoke(app, ["backfill-universe", "--help"])

    assert result.exit_code == 0
    assert "--confirm-full-market" in result.stdout
    assert "--force" in result.stdout
    assert "--no-confirm-full-market" not in result.stdout
    assert "--no-force" not in result.stdout


def test_all_without_confirmation_stops_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False
    settings = _universe_backfill_settings(profile="all")
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    def mark_opened(_settings: Settings) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("runtime must not open before the all-universe gate")

    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        mark_opened,
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 2
    assert opened is False
    assert _business_stderr(result) == "all universe requires --confirm-full-market\n"


def test_confirmation_is_invalid_outside_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    def mark_opened(_settings: Settings) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("runtime must not open for an invalid confirmation")

    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        mark_opened,
        raising=False,
    )

    result = runner.invoke(
        app,
        ["backfill-universe", "--confirm-full-market"],
    )

    assert result.exit_code == 2
    assert opened is False
    assert _business_stderr(result) == (
        "--confirm-full-market is only valid for all universe\n"
    )


def test_missing_as_of_stops_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False
    settings = _universe_backfill_settings(as_of=None)
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    def mark_opened(_settings: Settings) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("runtime must not open without an as-of date")

    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        mark_opened,
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 2
    assert opened is False
    assert _business_stderr(result) == (
        "backfill-universe requires DANG_BACKFILL_AS_OF\n"
    )


def test_all_confirmation_and_force_reach_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings(profile="all")
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(captured),
        raising=False,
    )

    result = runner.invoke(
        app,
        ["backfill-universe", "--confirm-full-market", "--force"],
    )

    assert result.exit_code == 0
    assert captured["settings"] is settings
    assert captured["planned_options"].force is True
    assert captured["run_options"].force is True


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("completed", 0),
        ("skipped", 0),
        ("partial", 4),
        ("failed", 4),
        ("locked", 5),
    ],
)
def test_backfill_universe_maps_stable_exit_codes(
    status: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(captured, status=status),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == exit_code
    assert json.loads(result.stdout)["status"] == status


def test_backfill_universe_writes_preflight_to_stderr_and_final_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(captured),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {
        "status": "completed",
        "jobRunId": "universe-run-1",
    }
    assert len(_business_stderr(result).splitlines()) == 1
    assert json.loads(_business_stderr(result)) == {
        "event": "backfill-universe.preflight",
        "asOf": "2026-09-12",
        "universeSize": 50,
    }


def test_backfill_universe_settings_failure_never_emits_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "invented-token-43891"
    database_url = "postgresql+psycopg://user:invented-password@db.invalid/app"

    def fail_settings(cls):
        raise ValueError(f"invalid settings: {token} {database_url}")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(fail_settings),
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 3
    assert _business_stderr(result) == "backfill-universe settings could not be loaded\n"
    assert result.stdout == ""
    combined_output = result.stdout + result.stderr
    assert token not in combined_output
    assert database_url not in combined_output
    assert "invented-password" not in combined_output


@pytest.mark.parametrize(
    "error_type",
    [UniverseConfigError, UniverseBackfillRuntimeError],
)
def test_backfill_universe_dependency_failure_is_redacted(
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    token = "invented-token-43891"
    database_url = "postgresql+psycopg://user:invented-password@db.invalid/app"
    settings = _universe_backfill_settings(
        database_url=database_url,
        token=token,
    )
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(
            captured,
            open_error=error_type(
                f"dependency failed: {token} "
                f"password=invented-password {database_url}"
            ),
        ),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "dependency failed" in result.stderr
    assert token not in result.stderr
    assert database_url not in result.stderr
    assert "invented-password" not in result.stderr
    assert "Traceback" not in result.stderr


def test_backfill_universe_planning_failure_uses_fixed_safe_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    secret = "planner-secret-context"
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(
            captured,
            plan_error=UniverseBackfillPlanningError(secret),
        ),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert _business_stderr(result) == "backfill-universe failed\n"
    assert secret not in result.stderr
    assert "Traceback" not in result.stderr


def test_backfill_universe_unknown_failure_uses_fixed_safe_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    secret = "unknown-secret-context"
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(
            captured,
            run_error=RuntimeError(secret),
        ),
        raising=False,
    )

    result = runner.invoke(app, ["backfill-universe"])

    assert result.exit_code == 4
    assert len(_business_stderr(result).splitlines()) == 2
    assert json.loads(_business_stderr(result).splitlines()[0])["event"] == (
        "backfill-universe.preflight"
    )
    assert _business_stderr(result).splitlines()[1] == "backfill-universe failed"
    assert result.stdout == ""
    assert secret not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("failure_stage", "exit_code"),
    [
        ("settings", 3),
        ("runtime", 3),
        ("planning", 4),
        ("job", 4),
    ],
)
def test_backfill_universe_exit_chains_never_retain_sensitive_errors(
    failure_stage: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    token = f"{failure_stage}-token"
    password = f"{failure_stage}-password"
    database_url = (
        f"postgresql+psycopg://user:{password}@db.invalid/app"
    )
    sensitive_values = (token, password, database_url)
    error_message = " ".join(sensitive_values)
    settings = _universe_backfill_settings(
        database_url=database_url,
        token=token,
    )

    if failure_stage == "settings":
        def fail_settings(cls):
            raise ValueError(error_message)

        monkeypatch.setattr(
            main_module.Settings,
            "from_env",
            classmethod(fail_settings),
        )
    else:
        monkeypatch.setattr(
            main_module.Settings,
            "from_env",
            classmethod(lambda cls: settings),
        )
        monkeypatch.setattr(
            main_module,
            "open_universe_backfill_runtime",
            _fake_universe_backfill_runtime(
                captured,
                open_error=(
                    UniverseBackfillRuntimeError(error_message)
                    if failure_stage == "runtime"
                    else None
                ),
                plan_error=(
                    UniverseBackfillPlanningError(error_message)
                    if failure_stage == "planning"
                    else None
                ),
                run_error=(
                    RuntimeError(error_message)
                    if failure_stage == "job"
                    else None
                ),
            ),
            raising=False,
        )

    with pytest.raises(typer.Exit) as captured_exit:
        main_module.backfill_universe(False, False)
    result = runner.invoke(app, ["backfill-universe"])

    assert captured_exit.value.exit_code == exit_code
    assert captured_exit.value.__cause__ is None
    assert captured_exit.value.__context__ is None
    assert result.exit_code == exit_code
    assert result.exception is not None
    for exit_error in (captured_exit.value, result.exception):
        chain_text = "\n".join(
            f"{type(error).__name__}: {error}"
            for error in _exception_chain(exit_error)
        )
        formatted = "".join(traceback.format_exception(exit_error))
        for sensitive_value in sensitive_values:
            assert sensitive_value not in chain_text
            assert sensitive_value not in formatted


def test_backfill_universe_does_not_catch_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(
            captured,
            open_error=KeyboardInterrupt(),
        ),
        raising=False,
    )

    callback = getattr(main_module, "backfill_universe", None)
    assert callback is not None
    with pytest.raises(KeyboardInterrupt):
        callback(False, False)


def test_backfill_universe_does_not_catch_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    settings = _universe_backfill_settings()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_universe_backfill_runtime",
        _fake_universe_backfill_runtime(
            captured,
            open_error=SystemExit(9),
        ),
        raising=False,
    )

    callback = getattr(main_module, "backfill_universe", None)
    assert callback is not None
    with pytest.raises(SystemExit) as captured_exit:
        callback(False, False)
    assert captured_exit.value.code == 9


def _sync_status_report(status: str) -> SyncStatusReport:
    return SyncStatusReport(
        status=status,
        checked_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        timezone="Asia/Shanghai",
        scheduler={
            "online": status == "ok",
            "instanceId": "scheduler-1",
            "state": "running",
            "startedAt": "2026-09-05T10:00:00Z",
            "lastHeartbeatAt": "2026-09-05T11:59:30Z",
            "nextRunAt": "2026-09-06T11:00:00Z",
        },
        sync={
            "expectedBusinessDate": "2026-09-05",
            "managedFrom": "2026-09-01",
            "universe": {
                "profile": "representative-50",
                "version": "2026-09-04-v1",
            },
            "lastRun": None,
            "consecutiveFailures": 0,
            "catchup": {
                "maxDays": 10,
                "backlogCount": 0,
                "missingBusinessDates": [],
                "catchupLimitExceeded": False,
            },
        },
        freshness={
            "checkedAt": "2026-09-05T12:00:00Z",
            "staleProducts": [],
            "missingProducts": [],
        },
        alerts=(),
    )


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("ok", 0), ("degraded", 1), ("unavailable", 3)],
)
def test_sync_status_outputs_one_json_document_and_expected_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    exit_code: int,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        tushare_token=None,
    )
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    @contextmanager
    def fake_builder(_settings: Settings):
        yield SimpleNamespace(check=lambda now: _sync_status_report(status))

    monkeypatch.setattr(main_module, "build_sync_status_service", fake_builder)

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == exit_code
    assert json.loads(result.stdout)["status"] == status
    assert result.stdout.count("\n") == 1
    assert _business_stderr(result) == ""


def test_sync_status_unknown_report_status_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        tushare_token=None,
    )
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    @contextmanager
    def fake_builder(_settings: Settings):
        yield SimpleNamespace(check=lambda now: _sync_status_report("unexpected"))

    monkeypatch.setattr(main_module, "build_sync_status_service", fake_builder)

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == 3
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert "unsupported sync status" in result.stderr


def test_sync_status_needs_no_token_and_never_opens_tushare_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        tushare_token=None,
    )
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )

    def unexpected(*_: object, **__: object) -> None:
        raise AssertionError("sync-status must not construct a Tushare runtime")

    monkeypatch.setattr(main_module, "TushareClient", unexpected)
    monkeypatch.setattr(main_module, "open_daily_sync_runtime", unexpected)

    @contextmanager
    def fake_builder(_settings: Settings):
        yield SimpleNamespace(check=lambda now: _sync_status_report("ok"))

    monkeypatch.setattr(main_module, "build_sync_status_service", fake_builder)

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "ok"


class _DisposableEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


def test_sync_status_settings_failure_uses_a_fixed_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (
        "postgresql+psycopg://owner:primary-password@primary.internal:5432/app"
    )
    read_database_url = (
        "postgresql+psycopg://reader:read-password@read.internal:5432/app"
    )
    token = "super-secret-token"

    def fail_settings(cls):
        raise ValueError(
            f"invalid settings: {database_url} {read_database_url} {token}"
        )

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(fail_settings),
    )

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == 3
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert _business_stderr(result) == "ValueError: sync-status settings could not be loaded\n"
    combined_output = result.stdout + result.stderr
    for secret in (
        database_url,
        read_database_url,
        "primary-password",
        "read-password",
        token,
    ):
        assert secret not in combined_output


def _sync_status_database_url(path: Path) -> str:
    database_url = f"sqlite+pysqlite:///{path.as_posix()}"
    engine, factory = create_engine_and_session(database_url)
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    with factory.begin() as session:
        session.add_all(
            [
                Security(
                    ts_code="600519.SH",
                    symbol="600519",
                    name="贵州茅台",
                    exchange="SSE",
                    status="L",
                    is_st=False,
                ),
                Security(
                    ts_code="000001.SZ",
                    symbol="000001",
                    name="平安银行",
                    exchange="SZSE",
                    status="D",
                    is_st=False,
                ),
                TradeCalendar(
                    exchange="SSE",
                    cal_date=date(2026, 9, 5),
                    is_open=True,
                    pretrade_date=date(2026, 9, 4),
                ),
            ]
        )
    engine.dispose()
    return database_url


def _sqlite_state(database_url: str) -> dict[str, tuple[tuple[str, ...], ...]]:
    engine, _factory = create_engine_and_session(database_url)
    try:
        metadata = MetaData()
        with engine.connect() as connection:
            table_names = sorted(sqlalchemy_inspect(connection).get_table_names())
            state: dict[str, tuple[tuple[str, ...], ...]] = {}
            for table_name in table_names:
                table = Table(table_name, metadata, autoload_with=connection)
                rows = connection.execute(select(table)).mappings()
                state[table_name] = tuple(
                    sorted(
                        tuple(f"{key}={value!r}" for key, value in row.items())
                        for row in rows
                    )
                )
            return state
    finally:
        engine.dispose()


@pytest.mark.parametrize("profile", ["all", "custom"])
def test_build_sync_status_service_uses_real_sqlite_read_only(
    tmp_path: Path,
    profile: str,
) -> None:
    database_url = _sync_status_database_url(tmp_path / f"sync-status-{profile}.db")
    universe_file = tmp_path / "custom-universe.json"
    universe_file.write_text('["600519.SH"]', encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        database_read_url=database_url,
        tushare_token=None,
        sync_universe=profile,
        sync_universe_file=universe_file if profile == "custom" else None,
    )
    before = _sqlite_state(database_url)

    with main_module.build_sync_status_service(settings) as service:
        payload = service.check(
            datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        ).to_payload()

    after = _sqlite_state(database_url)
    assert payload["status"] == "degraded"
    assert payload["sync"]["universe"]["profile"] == profile
    assert before == after


def test_sync_status_disposes_engine_when_assembly_fails_and_redacts_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql://user:secret@db.internal/status"
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        tushare_token=None,
    )
    engine = _DisposableEngine()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "create_engine_and_session",
        lambda url: (engine, object()),
    )

    class FailingUniverseResolver:
        def __init__(self, **_: object) -> None:
            pass

        def resolve(self, *_: object, **__: object) -> UniverseSelection:
            raise RuntimeError(f"cannot read {database_url}")

    monkeypatch.setattr(main_module, "UniverseResolver", FailingUniverseResolver)

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == 3
    assert json.loads(result.stdout) == {
        "status": "unavailable",
        "checkedAt": json.loads(result.stdout)["checkedAt"],
        "timezone": "Asia/Shanghai",
        "error": {
            "code": "SYNC_STATUS_UNAVAILABLE",
            "message": "sync status unavailable",
        },
    }
    assert "RuntimeError" in result.stderr
    assert database_url not in result.stdout
    assert database_url not in result.stderr
    assert "secret" not in result.stdout
    assert "secret" not in result.stderr
    assert engine.dispose_calls == 1


def test_sync_status_disposes_engine_when_check_fails_and_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql://user:password@db-primary.internal/status"
    read_database_url = (
        "postgresql://reader:read-password@db-read.internal/status"
    )
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        database_read_url=read_database_url,
        tushare_token="test-token",
    )
    engine = _DisposableEngine()
    selection = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "create_engine_and_session",
        lambda url: (engine, object()),
    )

    class FakeUniverseResolver:
        def __init__(self, **_: object) -> None:
            pass

        def resolve(self, *_: object, **__: object) -> UniverseSelection:
            return selection

    class FailingService:
        def check(self, now: datetime) -> SyncStatusReport:
            raise RuntimeError(
                f"token test-token failed at {database_url} via {read_database_url}"
            )

    monkeypatch.setattr(main_module, "UniverseResolver", FakeUniverseResolver)
    monkeypatch.setattr(main_module, "SyncStatusService", lambda **kwargs: FailingService())

    result = runner.invoke(app, ["sync-status"])

    assert result.exit_code == 3
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert "RuntimeError" in result.stderr
    assert "test-token" not in result.stdout
    assert "test-token" not in result.stderr
    assert database_url not in result.stdout
    assert database_url not in result.stderr
    assert read_database_url not in result.stdout
    assert read_database_url not in result.stderr
    assert "db-read.internal" not in result.stderr
    assert "password" not in result.stdout
    assert "password" not in result.stderr
    assert engine.dispose_calls == 1


def test_sync_daily_requires_explicit_business_date() -> None:
    result = runner.invoke(app, ["sync-daily"])

    assert result.exit_code == 2
    assert "business-date" in result.output.lower()


def test_sync_daily_rejects_invalid_universe_options_before_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "symbols.json"
    path.write_text('["600519.SH"]', encoding="utf-8")

    def unexpected_settings(cls):
        raise AssertionError("settings must not load for invalid universe options")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(unexpected_settings),
    )

    invalid_profile = runner.invoke(
        app,
        [
            "sync-daily",
            "--business-date",
            "2026-09-04",
            "--universe",
            "unknown-profile",
        ],
    )
    extra_file = runner.invoke(
        app,
        [
            "sync-daily",
            "--business-date",
            "2026-09-04",
            "--universe",
            "representative-50",
            "--universe-file",
            str(path),
        ],
    )

    assert invalid_profile.exit_code == 2
    assert "--universe" in invalid_profile.output
    assert extra_file.exit_code == 2
    assert "only valid for custom" in extra_file.output


def test_sync_daily_defaults_to_representative_and_reuses_one_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDailySyncJob:
        def run(self, options: object) -> DailySyncReport:
            captured["options"] = options
            return DailySyncReport(
                status="completed",
                job_run_id="daily-run-1",
                business_date=date(2026, 9, 4),
                mode=TushareMode.STANDARD,
                universe=selection,
                request_count=1,
                provider_count=3,
                retained_count=2,
                filtered_count=1,
                row_count=2,
            )

    selection = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )
    settings = Settings(
        data_backend="database",
        database_url="sqlite+pysqlite:///:memory:",
        tushare_token="test-token",
        tushare_mode="standard",
        tushare_page_size=123,
        sync_disclosure_lookback_days=9,
    )
    monkeypatch.setattr(main_module.Settings, "from_env", classmethod(lambda cls: settings))

    class FakeRuntimeContext:
        def __enter__(self) -> DailySyncRuntime:
            captured["runtime_entered"] = True
            return DailySyncRuntime(
                job=FakeDailySyncJob(),
                selection=selection,
                mode=TushareMode.STANDARD,
                session_factory=object(),
            )

        def __exit__(self, *_args: object) -> None:
            captured["runtime_closed"] = True

    def open_runtime(
        actual_settings: Settings,
        profile: str,
        universe_file: Path | None,
    ) -> FakeRuntimeContext:
        captured["runtime_args"] = (actual_settings, profile, universe_file)
        return FakeRuntimeContext()

    monkeypatch.setattr(
        main_module,
        "open_daily_sync_runtime",
        open_runtime,
        raising=False,
    )

    result = runner.invoke(
        app,
        ["sync-daily", "--business-date", "2026-09-04"],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["universe"]["profile"] == "representative-50"
    assert captured["runtime_args"] == (settings, "representative-50", None)
    assert captured["runtime_entered"] is True
    assert captured["runtime_closed"] is True
    assert captured["options"].execution.to_details() == {"trigger": "cli"}


def test_sync_daily_symbol_outside_universe_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    settings = Settings(
        data_backend="database",
        database_url="sqlite+pysqlite:///:memory:",
        tushare_token="test-token",
    )
    selection = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )

    class UnexpectedJob:
        def run(self, options: object) -> DailySyncReport:
            raise AssertionError("outside symbol must fail before job invocation")

    class FakeRuntimeContext:
        def __enter__(self) -> DailySyncRuntime:
            events.append("runtime.enter")
            return DailySyncRuntime(
                job=UnexpectedJob(),
                selection=selection,
                mode=TushareMode.STANDARD,
                session_factory=object(),
            )

        def __exit__(self, *_args: object) -> None:
            events.append("runtime.close")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        main_module,
        "open_daily_sync_runtime",
        lambda actual_settings, profile, universe_file: FakeRuntimeContext(),
    )

    result = runner.invoke(
        app,
        [
            "sync-daily",
            "--business-date",
            "2026-09-04",
            "--symbol",
            "000001.SZ",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout + _business_stderr(result) == (
        "symbol 000001.SZ is outside universe representative-50\n"
    )
    assert events == ["runtime.enter", "runtime.close"]


def test_backfill_requires_explicit_mode() -> None:
    result = runner.invoke(app, ["backfill", "--as-of", "2026-08-17"])

    assert result.exit_code != 0
    assert "mode" in result.output.lower()


def test_build_and_publish_cli_rejects_invalid_date() -> None:
    result = runner.invoke(app, ["build-and-publish", "--as-of", "bad"])

    assert result.exit_code == 2
    assert "--as-of 必须是 YYYY-MM-DD" in result.output


def test_build_and_publish_cli_redacts_database_password(monkeypatch) -> None:
    database_url = "postgresql+psycopg://smartparse:super-secret@127.0.0.1:5432/smartparse"

    def fail_from_env(cls):
        raise ValueError(f"cannot connect to {database_url}")

    monkeypatch.setattr(main_module.Settings, "from_env", classmethod(fail_from_env))

    result = runner.invoke(app, ["build-and-publish", "--as-of", "2026-08-24"])

    assert result.exit_code == 4
    assert "super-secret" not in result.output
    assert "postgresql+psycopg://***@127.0.0.1:5432/smartparse" in result.output


def test_backfill_requires_at_least_one_dataset() -> None:
    result = runner.invoke(
        app,
        ["backfill", "--mode", "standard", "--as-of", "2026-08-17"],
    )

    assert result.exit_code == 2
    assert "--dataset" in result.output


def test_backfill_rejects_disclosure_date_until_backfill_supports_it() -> None:
    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "disclosure_date",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 2
    assert "unsupported dataset: disclosure_date" in result.output


@pytest.mark.parametrize(
    ("financial_start", "message"),
    [
        ("not-a-date", "--financial-start 必须是 YYYY-MM-DD"),
        ("2026-08-18", "--financial-start 不能晚于 --as-of"),
    ],
)
def test_backfill_rejects_invalid_financial_start(
    financial_start: str,
    message: str,
) -> None:
    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--financial-start",
            financial_start,
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 2
    assert message in result.output


def test_backfill_settings_failure_exits_three_with_sanitized_json(monkeypatch) -> None:
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", "test-token")

    def fail_from_env(cls):
        raise ValueError("invalid DANG_TUSHARE_PAGE_SIZE for token test-token")

    monkeypatch.setattr(main_module.Settings, "from_env", classmethod(fail_from_env))

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 3
    assert json.loads(_business_stderr(result)) == {
        "status": "error",
        "errorType": "ValueError",
        "message": "invalid DANG_TUSHARE_PAGE_SIZE for token ***",
    }
    assert "test-token" not in result.output


def test_backfill_settings_failure_redacts_token_from_configured_env_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token = "file-only-token"
    env_file = tmp_path / "cli-test.env"
    env_file.write_text(
        f"DANG_TUSHARE_TOKEN={token}\nDANG_TUSHARE_PAGE_SIZE=0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", env_file)
    monkeypatch.delenv("DANG_TUSHARE_TOKEN", raising=False)
    original_from_env = main_module.Settings.from_env

    def fail_from_env(cls):
        try:
            original_from_env()
        except Exception as exc:
            raise ValueError(f"settings construction failed for token {token}") from exc
        raise AssertionError("expected invalid page-size settings")

    monkeypatch.setattr(main_module.Settings, "from_env", classmethod(fail_from_env))

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 3
    assert json.loads(_business_stderr(result)) == {
        "status": "error",
        "errorType": "ValueError",
        "message": "settings construction failed for token ***",
    }
    assert token not in result.output


def test_smoke_is_skipped_without_token(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: Settings(_env_file=None, tushare_token=None)),
    )
    result = runner.invoke(app, ["smoke-tushare"])

    assert result.exit_code == 2
    assert "Token" in result.output


def test_smoke_tushare_calls_one_day_trade_calendar(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    class FakeClient:
        def __init__(self, token: str) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            calls.append((api_name, params, fields))
            return TushareResponse(
                fields=("exchange", "cal_date", "is_open", "pretrade_date"),
                items=(("SSE", "20260819", 1, "20260818"),),
            )

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: Settings(_env_file=None, tushare_token="test-token")),
    )
    monkeypatch.setattr(main_module, "TushareClient", FakeClient, raising=False)

    result = runner.invoke(app, ["smoke-tushare"])

    assert result.exit_code == 0
    assert result.output.startswith("{")
    assert json.loads(result.stdout) == {
        "status": "ok",
        "api": "trade_cal",
        "rows": 1,
    }
    today = date.today().strftime("%Y%m%d")
    assert calls == [
        (
            "trade_cal",
            {"exchange": "SSE", "start_date": today, "end_date": today},
            ("exchange", "cal_date", "is_open", "pretrade_date"),
        )
    ]


def test_smoke_tushare_reports_provider_error_without_token(monkeypatch) -> None:
    class FailingClient:
        def __init__(self, token: str) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FailingClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            raise TushareAuthError("token test-token invalid")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(lambda cls: Settings(_env_file=None, tushare_token="test-token")),
    )
    monkeypatch.setattr(main_module, "TushareClient", FailingClient, raising=False)

    result = runner.invoke(app, ["smoke-tushare"])

    assert result.exit_code == 3
    payload = json.loads(_business_stderr(result))
    assert payload["status"] == "error"
    assert payload["errorType"] == "TushareAuthError"
    assert "test-token" not in result.output


def _database_url(path: Path) -> str:
    url = f"sqlite+pysqlite:///{path.as_posix()}"
    engine, _factory = create_engine_and_session(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _successful_report(*, results: tuple[object, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        job_run_id="run-1",
        planned=len(results),
        completed=len(results),
        failed=0,
        rejected=sum(int(getattr(result, "rejected_count", 0)) for result in results),
        results=results,
        failures=(),
    )


def _batch_result(
    *,
    batch_id: str,
    dataset: Dataset = Dataset.DAILY,
    received_count: int,
    row_count: int | None = None,
    rejected_count: int = 0,
) -> BatchResult:
    return BatchResult(
        batch_id=batch_id,
        dataset=dataset,
        received_count=received_count,
        row_count=received_count - rejected_count if row_count is None else row_count,
        rejected_count=rejected_count,
    )


def test_backfill_wires_rate_limit_and_page_size_by_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEngine:
        def dispose(self) -> None:
            captured["disposed"] = True

    class FakeRateLimiter:
        def __init__(self, requests_per_minute: int) -> None:
            captured["requests_per_minute"] = requests_per_minute

    class FakeClient:
        def __init__(self, token: str, *, rate_limiter: object | None = None) -> None:
            captured["token"] = token
            captured["rate_limiter"] = rate_limiter

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class FakePipeline:
        def __init__(self, client: FakeClient, factory: object) -> None:
            captured["pipeline_client"] = client
            captured["pipeline_factory"] = factory

        def run(self, page_plan: object, *, mode: str) -> BatchResult:
            raise AssertionError("backfill job fake should not execute page plans")

    class FakeCursorRepository:
        def __init__(self, factory: object) -> None:
            captured["cursor_factory"] = factory

    class FakePaginatedPlanRunner:
        def __init__(
            self,
            run_page: object,
            cursor_store: object,
            *,
            mode: object,
            page_size: int,
        ) -> None:
            captured["page_runner"] = self
            captured["run_page"] = run_page
            captured["cursor_store"] = cursor_store
            captured["page_runner_mode"] = mode
            captured["page_size"] = page_size

    class FakeBackfillJob:
        def __init__(self, runner: object, **_: object) -> None:
            captured["job_runner"] = runner

        def run(self, options: object, *_: object) -> SimpleNamespace:
            captured["financial_start"] = options.financial_start
            return _successful_report()

    factory = object()
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url="sqlite+pysqlite:///:memory:",
                tushare_token="test-token",
                tushare_standard_requests_per_minute=17,
                tushare_vip_requests_per_minute=88,
                tushare_page_size=1234,
            )
        ),
    )
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda _: (FakeEngine(), factory))
    monkeypatch.setattr(main_module, "RequestRateLimiter", FakeRateLimiter, raising=False)
    monkeypatch.setattr(main_module, "TushareClient", FakeClient)
    monkeypatch.setattr(main_module, "IngestionPipeline", FakePipeline)
    monkeypatch.setattr(main_module, "SqlJobCursorRepository", FakeCursorRepository, raising=False)
    monkeypatch.setattr(main_module, "PaginatedPlanRunner", FakePaginatedPlanRunner, raising=False)
    monkeypatch.setattr(main_module, "BackfillJob", FakeBackfillJob)

    standard = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--financial-start",
            "2024-01-01",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert standard.exit_code == 0
    assert captured["requests_per_minute"] == 17
    assert captured["rate_limiter"] is not None
    assert captured["job_runner"] is captured["page_runner"]
    assert captured["cursor_factory"] is factory
    assert captured["page_runner_mode"].value == "standard"
    assert captured["page_size"] == 1234
    assert captured["financial_start"] == date(2024, 1, 1)
    assert captured["disposed"] is True

    captured.clear()
    import app.stocks.marts.metric_cards as card_publication
    def rebuild_cards(_factory, *, symbols, dry_run, shareholder_returns=False):
        assert shareholder_returns is True
        captured["card_scope"] = symbols
        assert not dry_run
        return {"status": "completed"}
    monkeypatch.setattr(card_publication, "rebuild_metric_cards", rebuild_cards)
    vip = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "vip",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "income",
            "--symbol",
            "600519.SH",
            "--publish",
        ],
    )

    assert vip.exit_code == 0
    assert captured["card_scope"] == ()
    assert captured["requests_per_minute"] == 88
    assert captured["page_runner_mode"].value == "vip"
    assert captured["page_size"] == 1234


def test_backfill_json_includes_paginated_plans_and_flattened_batches(monkeypatch) -> None:
    paginated = PaginatedResult(
        dataset=Dataset.DAILY,
        request_key="daily:600519.SH",
        pages=2,
        request_count=2,
        resumed_from_offset=5000,
        batches=(
            _batch_result(batch_id="uuid-1", received_count=500, row_count=500),
            _batch_result(batch_id="uuid-2", received_count=226, row_count=226),
        ),
        completed=True,
    )

    class FakeEngine:
        def dispose(self) -> None:
            return None

    class FakeClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class FakeBackfillJob:
        def __init__(self, *_: object, **__: object) -> None:
            return None

        def run(self, *_: object) -> SimpleNamespace:
            return _successful_report(results=(paginated,))

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url="sqlite+pysqlite:///:memory:",
                tushare_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda _: (FakeEngine(), object()))
    monkeypatch.setattr(main_module, "TushareClient", FakeClient)
    monkeypatch.setattr(main_module, "BackfillJob", FakeBackfillJob)

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["jobRunId"] == "run-1"
    assert payload["planned"] == 1
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    assert payload["rejected"] == 0
    assert payload["plans"] == [
        {
            "dataset": "daily",
            "requestKey": "daily:600519.SH",
            "pages": 2,
            "requestCount": 2,
            "resumedFromOffset": 5000,
            "rowCount": 726,
            "rejectedCount": 0,
            "batchIds": ["uuid-1", "uuid-2"],
            "completed": True,
        }
    ]
    assert payload["batches"] == [
        {
            "batchId": "uuid-1",
            "dataset": "daily",
            "receivedCount": 500,
            "rowCount": 500,
            "rejectedCount": 0,
        },
        {
            "batchId": "uuid-2",
            "dataset": "daily",
            "receivedCount": 226,
            "rowCount": 226,
            "rejectedCount": 0,
        },
    ]
    assert payload["failures"] == []


def test_backfill_partial_paginated_error_prints_aggregate_exits_four_and_redacts_token(monkeypatch) -> None:
    paginated = PaginatedResult(
        dataset=Dataset.DAILY,
        request_key="daily:600519.SH",
        pages=1,
        request_count=1,
        resumed_from_offset=5000,
        batches=(
            _batch_result(
                batch_id="uuid-partial",
                received_count=5,
                row_count=4,
                rejected_count=1,
            ),
        ),
        completed=False,
    )

    class FakeEngine:
        def dispose(self) -> None:
            return None

    class FakeClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class FakeBackfillJob:
        def __init__(self, *_: object, sanitize_error, **__: object) -> None:
            self._sanitize_error = sanitize_error

        def run(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                job_run_id="run-1",
                planned=1,
                completed=0,
                failed=1,
                rejected=1,
                results=(paginated,),
                failures=(
                    PlanFailure(
                        dataset=Dataset.DAILY,
                        request_key="daily:600519.SH",
                        error_code="TushareRateLimitError",
                        message=self._sanitize_error("rate limit for token test-token"),
                        retryable=True,
                    ),
                ),
            )

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url="sqlite+pysqlite:///:memory:",
                tushare_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(main_module, "create_engine_and_session", lambda _: (FakeEngine(), object()))
    monkeypatch.setattr(main_module, "TushareClient", FakeClient)
    monkeypatch.setattr(main_module, "BackfillJob", FakeBackfillJob)

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["plans"] == [
        {
            "dataset": "daily",
            "requestKey": "daily:600519.SH",
            "pages": 1,
            "requestCount": 1,
            "resumedFromOffset": 5000,
            "rowCount": 4,
            "rejectedCount": 1,
            "batchIds": ["uuid-partial"],
            "completed": False,
        }
    ]
    assert payload["batches"] == [
        {
            "batchId": "uuid-partial",
            "dataset": "daily",
            "receivedCount": 5,
            "rowCount": 4,
            "rejectedCount": 1,
        }
    ]
    assert payload["failures"] == [
        {
            "dataset": "daily",
            "requestKey": "daily:600519.SH",
            "errorCode": "TushareRateLimitError",
            "message": "rate limit for token ***",
            "retryable": True,
        }
    ]
    assert "test-token" not in result.output


def test_backfill_executes_pipeline_and_prints_batch_summary(monkeypatch, tmp_path) -> None:
    database_url = _database_url(tmp_path / "backfill-success.db")

    class FakeClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            assert params["ts_code"] in {"600519.SH", "000001.SZ"}
            assert params["start_date"] == "20230817"
            assert params["end_date"] == "20260817"
            if api_name == "daily":
                return TushareResponse(
                    fields=fields,
                    items=((
                        params["ts_code"],
                        "20260817",
                        "9.50",
                        "11.20",
                        "9.10",
                        "10.00",
                        "9.80",
                        "0.20",
                        "2.04",
                        "100",
                        "1000",
                    ),),
                )
            assert api_name == "adj_factor"
            return TushareResponse(
                fields=fields,
                items=((params["ts_code"], "20260817", "1.25"),),
            )

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(main_module, "TushareClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--dataset",
            "adj_factor",
            "--symbol",
            "600519.SH",
            "--symbol",
            "000001.SZ",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["planned"] == 4
    assert payload["completed"] == 4
    assert payload["failed"] == 0
    assert payload["rejected"] == 0
    assert [batch["dataset"] for batch in payload["batches"]] == [
        "daily",
        "daily",
        "adj_factor",
        "adj_factor",
    ]
    assert all(batch["batchId"] for batch in payload["batches"])
    assert all(batch["rowCount"] == 1 for batch in payload["batches"])
    assert all(batch["rejectedCount"] == 0 for batch in payload["batches"])

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        assert set(session.scalars(select(DailyPrice.ts_code))) == {
            "600519.SH",
            "000001.SZ",
        }
        assert set(session.scalars(select(AdjustmentFactor.ts_code))) == {
            "600519.SH",
            "000001.SZ",
        }
        assert set(session.scalars(select(IngestionBatch.mode))) == {"standard"}
        assert session.scalar(select(JobRun.status)) == "completed"
    engine.dispose()


def test_backfill_persists_provider_failure_and_exits_nonzero(monkeypatch, tmp_path) -> None:
    database_url = _database_url(tmp_path / "backfill-failure.db")

    class FailingClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FailingClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            raise TushareTransientError("token test-token temporary provider outage")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(main_module, "TushareClient", FailingClient)

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["planned"] == 1
    assert payload["completed"] == 0
    assert payload["failed"] == 1
    assert payload["failures"] == [{
        "dataset": "daily",
        "requestKey": "daily:600519.SH",
        "errorCode": "TushareTransientError",
        "message": "token *** temporary provider outage",
        "retryable": True,
    }]

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        failure = session.scalar(select(JobFailure))
        assert session.scalar(select(JobRun.status)) == "failed"
    assert failure is not None
    assert failure.request_key == "daily:600519.SH"
    assert failure.message == "token *** temporary provider outage"
    assert "test-token" not in failure.message
    engine.dispose()


def test_retry_failures_replays_a_persisted_request_and_resolves_it(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path / "retry-failures.db")
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
            )
        ),
    )

    class FailingClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FailingClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            raise TushareTransientError("temporary provider outage")

    monkeypatch.setattr(main_module, "TushareClient", FailingClient)
    failed_backfill = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )
    assert failed_backfill.exit_code == 4
    job_run_id = json.loads(failed_backfill.stdout)["jobRunId"]

    class SuccessfulClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "SuccessfulClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            assert api_name == "daily"
            assert params == {
                "ts_code": "600519.SH",
                "start_date": "20230817",
                "end_date": "20260817",
                "limit": "5000",
                "offset": "0",
            }
            return TushareResponse(
                fields=fields,
                items=((
                    "600519.SH",
                    "20260817",
                    "9.50",
                    "11.20",
                    "9.10",
                    "10.00",
                    "9.80",
                    "0.20",
                    "2.04",
                    "100",
                    "1000",
                ),),
            )

    monkeypatch.setattr(main_module, "TushareClient", SuccessfulClient)
    retried = runner.invoke(
        app,
        [
            "retry-failures",
            "--job-run-id",
            job_run_id,
            "--dataset",
            "daily",
            "--error-code",
            "TushareTransientError",
        ],
    )

    assert retried.exit_code == 0
    payload = json.loads(retried.stdout)
    assert payload["status"] == "completed"
    assert payload["selected"] == 1
    assert payload["attempted"] == 1
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    assert payload["skipped"] == 0
    assert payload["outcomes"] == [
        {
            "failureId": 1,
            "jobRunId": job_run_id,
            "dataset": "daily",
            "requestKey": "daily:600519.SH",
            "status": "completed",
            "attempts": 2,
            "errorCode": None,
            "message": None,
            "retryable": False,
        }
    ]
    assert payload["plans"][0]["requestKey"] == "daily:600519.SH"
    assert payload["plans"][0]["completed"] is True
    assert payload["batches"][0]["rowCount"] == 1

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        failure = session.scalar(select(JobFailure))
        daily_price = session.scalar(select(DailyPrice))
    assert failure is not None
    assert failure.attempts == 2
    assert failure.last_attempted_at is not None
    assert failure.resolved_at is not None
    assert daily_price is not None
    assert daily_price.ts_code == "600519.SH"
    engine.dispose()


def test_retry_failures_resumes_the_saved_offset_with_the_original_page_size(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path / "retry-resume.db")

    def settings_with_page_size(page_size: int):
        return classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
                tushare_page_size=page_size,
            )
        )

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        settings_with_page_size(2),
    )

    def daily_row(trade_date: str, close: str) -> tuple[str, ...]:
        return (
            "600519.SH",
            trade_date,
            "9.50",
            "11.20",
            "9.10",
            close,
            "9.80",
            "0.20",
            "2.04",
            "100",
            "1000",
        )

    class SecondPageFailureClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "SecondPageFailureClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            assert api_name == "daily"
            assert params["limit"] == "2"
            if params["offset"] == "0":
                return TushareResponse(
                    fields=fields,
                    items=(
                        daily_row("20260815", "10.00"),
                        daily_row("20260816", "10.10"),
                    ),
                )
            assert params["offset"] == "2"
            raise TushareTransientError("temporary second-page outage")

    monkeypatch.setattr(main_module, "TushareClient", SecondPageFailureClient)
    failed_backfill = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )
    assert failed_backfill.exit_code == 4
    job_run_id = json.loads(failed_backfill.stdout)["jobRunId"]

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        cursor = session.scalar(select(JobCursor))
        failure = session.scalar(select(JobFailure))
    assert cursor is not None
    assert cursor.cursor_value["offset"] == 2
    assert cursor.cursor_value["pageSize"] == 2
    assert failure is not None
    assert failure.request_payload["pageSize"] == 2
    engine.dispose()

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        settings_with_page_size(5000),
    )

    class ResumedClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "ResumedClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            assert api_name == "daily"
            assert params["limit"] == "2"
            assert params["offset"] == "2"
            return TushareResponse(
                fields=fields,
                items=(daily_row("20260817", "10.20"),),
            )

    monkeypatch.setattr(main_module, "TushareClient", ResumedClient)
    retried = runner.invoke(
        app,
        ["retry-failures", "--job-run-id", job_run_id],
    )

    assert retried.exit_code == 0
    payload = json.loads(retried.stdout)
    assert payload["status"] == "completed"
    assert payload["plans"][0]["resumedFromOffset"] == 2
    assert payload["plans"][0]["pages"] == 1
    assert payload["plans"][0]["requestCount"] == 1

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        cursor = session.scalar(select(JobCursor))
        failure = session.scalar(select(JobFailure))
        dates = tuple(session.scalars(select(DailyPrice.trade_date).order_by(DailyPrice.trade_date)))
    assert cursor is None
    assert failure is not None
    assert failure.resolved_at is not None
    assert len(dates) == 3
    engine.dispose()


def test_retry_failures_preserves_unretryable_failures_and_exits_four(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path / "retry-unretryable.db")
    engine, factory = create_engine_and_session(database_url)
    with factory.begin() as session:
        session.add(
            JobRun(
                id="run-unretryable",
                job_name="backfill",
                business_key="2026-08-17:standard",
                status="failed",
            )
        )
        session.add(
            JobFailure(
                job_run_id="run-unretryable",
                dataset="daily",
                request_key="daily:600519.SH",
                error_code="TusharePermissionError",
                message="permission denied",
                retryable=False,
                request_payload={},
            )
        )
    engine.dispose()

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token=None,
            )
        ),
    )

    class UnexpectedClient:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("unretryable failures must not call Tushare")

    monkeypatch.setattr(main_module, "TushareClient", UnexpectedClient)

    result = runner.invoke(
        app,
        ["retry-failures", "--job-run-id", "run-unretryable"],
    )

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["selected"] == 1
    assert payload["attempted"] == 0
    assert payload["completed"] == 0
    assert payload["failed"] == 0
    assert payload["skipped"] == 1
    assert payload["outcomes"][0]["errorCode"] == "TusharePermissionError"
    assert payload["outcomes"][0]["message"] == "permission denied"

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        failure = session.scalar(select(JobFailure))
    assert failure is not None
    assert failure.attempts == 1
    assert failure.resolved_at is None
    engine.dispose()


@pytest.mark.parametrize(
    ("request_payload", "expected_exit_code", "expected_error_code"),
    [
        ({}, 4, "InvalidReplayRequest"),
        (
            serialize_replay_request(
                build_request_plans(
                    Dataset.SECURITY,
                    TushareMode.STANDARD,
                    SyncScope(),
                )[0],
                TushareMode.STANDARD,
            ),
            3,
            None,
        ),
    ],
)
def test_retry_failures_without_token_never_attempts_or_changes_a_retryable_record(
    monkeypatch,
    tmp_path,
    request_payload,
    expected_exit_code,
    expected_error_code,
) -> None:
    database_url = _database_url(tmp_path / f"retry-no-token-{expected_exit_code}.db")
    engine, factory = create_engine_and_session(database_url)
    with factory.begin() as session:
        session.add(
            JobRun(
                id="run-no-token",
                job_name="backfill",
                business_key="2026-08-17:standard",
                status="failed",
            )
        )
        session.add(
            JobFailure(
                job_run_id="run-no-token",
                dataset="security",
                request_key="security:all",
                error_code="TushareTransientError",
                message="temporary outage",
                retryable=True,
                request_payload=request_payload,
            )
        )
    engine.dispose()

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token=None,
            )
        ),
    )

    class UnexpectedClient:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("missing token must not create a Tushare client")

    monkeypatch.setattr(main_module, "TushareClient", UnexpectedClient)

    result = runner.invoke(
        app,
        ["retry-failures", "--job-run-id", "run-no-token"],
    )

    assert result.exit_code == expected_exit_code
    if expected_error_code is None:
        assert "DANG_TUSHARE_TOKEN" in result.output
    else:
        payload = json.loads(result.stdout)
        assert payload["outcomes"][0]["errorCode"] == expected_error_code
        assert payload["skipped"] == 1

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        failure = session.scalar(select(JobFailure))
    assert failure is not None
    assert failure.attempts == 1
    assert failure.last_attempted_at is None
    assert failure.resolved_at is None
    engine.dispose()


def test_retry_failures_redacts_token_when_the_provider_fails_again(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path / "retry-redaction.db")
    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
            )
        ),
    )

    class FailingClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FailingClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            raise TushareTransientError("provider failed for token test-token")

    monkeypatch.setattr(main_module, "TushareClient", FailingClient)
    failed_backfill = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )
    job_run_id = json.loads(failed_backfill.stdout)["jobRunId"]

    retried = runner.invoke(
        app,
        ["retry-failures", "--job-run-id", job_run_id],
    )

    assert retried.exit_code == 4
    assert "test-token" not in retried.output
    payload = json.loads(retried.stdout)
    assert payload["status"] == "failed"
    assert payload["attempted"] == 1
    assert payload["failed"] == 1
    assert payload["outcomes"][0]["message"] == "provider failed for token ***"
    assert payload["outcomes"][0]["attempts"] == 2

    engine, factory = create_engine_and_session(database_url)
    with factory() as session:
        failure = session.scalar(select(JobFailure))
    assert failure is not None
    assert failure.message == "provider failed for token ***"
    assert failure.attempts == 2
    assert failure.resolved_at is None
    engine.dispose()


def test_retry_failures_rejects_an_unknown_dataset() -> None:
    result = runner.invoke(app, ["retry-failures", "--dataset", "unknown"])

    assert result.exit_code == 2
    assert "unsupported dataset" in result.output


def test_backfill_reports_sanitized_history_startup_failure(monkeypatch, tmp_path) -> None:
    database_url = _database_url(tmp_path / "backfill-history-failure.db")

    class FakeClient:
        def __init__(self, token: str, **_: object) -> None:
            assert token == "test-token"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def call(self, api_name, params, fields) -> TushareResponse:
            raise AssertionError("request must not start without job history")

    monkeypatch.setattr(
        main_module.Settings,
        "from_env",
        classmethod(
            lambda cls: Settings(
                _env_file=None,
                database_url=database_url,
                tushare_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(main_module, "TushareClient", FakeClient)

    def fail_start(self, options, mode) -> str:
        raise RuntimeError("database failed for token test-token")

    monkeypatch.setattr(main_module.SqlBackfillHistory, "start", fail_start)

    result = runner.invoke(
        app,
        [
            "backfill",
            "--mode",
            "standard",
            "--as-of",
            "2026-08-17",
            "--dataset",
            "daily",
            "--symbol",
            "600519.SH",
        ],
    )

    assert result.exit_code == 3
    assert json.loads(_business_stderr(result)) == {
        "status": "error",
        "errorType": "RuntimeError",
        "message": "database failed for token ***",
    }
    assert "test-token" not in result.output
