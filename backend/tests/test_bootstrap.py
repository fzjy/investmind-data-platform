import json
from copy import deepcopy
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bootstrap import (
    BootstrapCommandError,
    BootstrapConfigurationError,
    BootstrapOptions,
    BootstrapRunner,
    ReadinessResult,
    SnapshotReadinessChecker,
    _redact,
    build_backfill_args,
    build_publish_args,
    expected_products,
    main,
)
from app.config import Settings
from app.db.base import MartBase
from app.db.models import PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
from backend.tests.support.fixtures import FixtureStore


def _seed_ready_release(
    factory: sessionmaker,
    symbols: tuple[str, ...],
    version: str = "v1",
) -> None:
    published_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    with factory.begin() as session:
        for product in expected_products(symbols):
            payload = _valid_fixture_payload(product)
            session.add(PublishedSnapshot(
                product=product, version=version, as_of=published_at, payload=payload,
            ))
            session.add(PageSnapshot(
                product=product, snapshot_version=version,
                as_of=published_at, payload=payload,
            ))
        for symbol in symbols:
            session.add(StockWorkbenchSnapshot(
                ts_code=symbol, as_of=date(2026, 8, 30),
                snapshot_version=version, payload={"symbol": symbol},
            ))


def _valid_fixture_payload(product: str) -> dict[str, object]:
    fixture_name = product.split(":", 1)[0]
    payload = deepcopy(FixtureStore.default().read(fixture_name).raw)
    if fixture_name == "stock-workbench":
        workbench = next(iter(payload.values()))
        assert isinstance(workbench, dict)
        return workbench
    if fixture_name == "portfolio":
        assets = payload.get("assets")
        assert isinstance(assets, list)
        for asset in assets:
            assert isinstance(asset, dict)
            asset["symbol"] = asset["code"]
    return payload


class SequenceChecker:
    def __init__(self, *results: ReadinessResult) -> None:
        self.results = list(results)

    def check(self, symbols: tuple[str, ...]) -> ReadinessResult:
        assert symbols == ("600519.SH",)
        return self.results.pop(0)


class RecordingCommands:
    def __init__(self, *exit_codes: int) -> None:
        self.exit_codes = list(exit_codes)
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...]) -> int:
        self.calls.append(args)
        return self.exit_codes.pop(0)


def _options(force: bool = False) -> BootstrapOptions:
    return BootstrapOptions(("600519.SH",), date(2026, 8, 24), force, "standard")


def test_bootstrap_options_use_timezone_date_and_normalize_symbols() -> None:
    settings = Settings(
        _env_file=None,
        bootstrap_symbols=" 600519.sh,000001.sz,600519.SH ",
        sync_timezone="Asia/Shanghai",
        tushare_mode="vip",
    )
    options = BootstrapOptions.from_settings(
        settings,
        now=datetime(2026, 8, 30, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert options == BootstrapOptions(
        symbols=("600519.SH", "000001.SZ"),
        as_of=date(2026, 8, 30),
        force=False,
        mode="vip",
    )


def test_bootstrap_options_honor_explicit_date_and_force() -> None:
    settings = Settings(
        _env_file=None,
        bootstrap_as_of=date(2026, 8, 24),
        bootstrap_force=True,
    )
    options = BootstrapOptions.from_settings(settings)
    assert options.as_of == date(2026, 8, 24)
    assert options.force is True


def test_bootstrap_options_reject_unknown_timezone() -> None:
    settings = Settings(_env_file=None, sync_timezone="Mars/Olympus")
    with pytest.raises(BootstrapConfigurationError, match="DANG_SYNC_TIMEZONE"):
        BootstrapOptions.from_settings(settings)


@pytest.mark.parametrize(
    "timezone_key",
    ["/etc/passwd", "../Asia/Shanghai", "C:/secret/timezone"],
)
def test_bootstrap_options_reject_path_like_timezone_without_echoing_value(
    timezone_key: str,
) -> None:
    settings = Settings(_env_file=None, sync_timezone=timezone_key)

    with pytest.raises(BootstrapConfigurationError) as exc_info:
        BootstrapOptions.from_settings(settings)

    assert str(exc_info.value) == "DANG_SYNC_TIMEZONE 不合法"
    assert timezone_key not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("bootstrap_as_of", "not-a-date"), ("bootstrap_force", "not-a-bool")],
)
def test_settings_reject_invalid_bootstrap_scalars(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("symbols", ["", "600519", "600519.SH,"])
def test_bootstrap_options_reject_invalid_symbol_lists(symbols: str) -> None:
    with pytest.raises(BootstrapConfigurationError):
        BootstrapOptions.from_settings(
            Settings(_env_file=None, bootstrap_symbols=symbols)
        )


def test_bootstrap_builds_fixed_cli_arguments() -> None:
    options = BootstrapOptions(
        symbols=("600519.SH", "000001.SZ"),
        as_of=date(2026, 8, 24),
        force=False,
        mode="standard",
    )
    assert build_backfill_args(options) == (
        "backfill", "--mode", "standard", "--as-of", "2026-08-24",
        "--dataset", "security", "--dataset", "daily",
        "--dataset", "daily_basic", "--dataset", "adj_factor",
        "--dataset", "income", "--dataset", "balance_sheet",
        "--dataset", "cashflow", "--dataset", "financial_indicator",
        "--symbol", "600519.SH", "--symbol", "000001.SZ",
    )
    assert build_publish_args(options) == (
        "build-and-publish", "--as-of", "2026-08-24",
        "--symbol", "600519.SH", "--symbol", "000001.SZ",
    )
    assert expected_products(options.symbols) == (
        "market-overview", "market-explorer", "macro-transmission",
        "macro-insights", "screener", "portfolio",
        "stock-workbench:600519.SH", "stock-workbench:000001.SZ",
    )


def test_readiness_requires_all_products_and_one_version(pg_factory) -> None:
    factory = pg_factory
    symbols = ("600519.SH", "000001.SZ")
    _seed_ready_release(factory, symbols)
    result = SnapshotReadinessChecker(factory).check(symbols)
    assert result.ready is True
    assert result.version == "v1"
    assert result.problems == ()


def test_readiness_reports_missing_product(pg_factory) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        session.delete(session.get(PublishedSnapshot, "portfolio"))
    result = SnapshotReadinessChecker(factory).check(("600519.SH",))
    assert result.ready is False
    assert "missing published product: portfolio" in result.problems


@pytest.mark.parametrize("macro_version", [None, "macro-v2"])
def test_bootstrap_skips_securities_with_independent_or_unpublished_macro(pg_factory, macro_version: str | None) -> None:
    factory = pg_factory
    symbols = ("600519.SH",)
    _seed_ready_release(factory, symbols)
    with factory.begin() as session:
        for product in ("macro-transmission", "macro-insights"):
            pointer = session.get(PublishedSnapshot, product)
            assert pointer is not None
            if macro_version is None:
                session.delete(pointer)
            else:
                pointer.version = macro_version
                session.add(PageSnapshot(product=product, snapshot_version=macro_version, as_of=pointer.as_of, payload=pointer.payload))
    readiness = SnapshotReadinessChecker(factory)
    assert readiness.check(symbols).ready is True
    commands = RecordingCommands()
    report = BootstrapRunner(readiness, commands).run(_options())
    assert report.status == "skipped"
    assert report.version == "v1"
    assert commands.calls == []


@pytest.mark.parametrize("fault", ["one-missing", "mismatch", "missing-history"])
def test_readiness_rejects_broken_macro_domain(pg_factory, fault: str) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        pointer = session.get(PublishedSnapshot, "macro-insights")
        assert pointer is not None
        if fault == "one-missing":
            session.delete(pointer)
        elif fault == "mismatch":
            pointer.version = "macro-v2"
        else:
            for product in ("macro-transmission", "macro-insights"):
                session.get(PublishedSnapshot, product).version = "macro-v2"
    assert SnapshotReadinessChecker(factory).check(("600519.SH",)).ready is False


def test_readiness_rejects_mixed_versions(pg_factory) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        session.get(PublishedSnapshot, "portfolio").version = "v2"
    result = SnapshotReadinessChecker(factory).check(("600519.SH",))
    assert result.ready is False
    assert "published products use multiple versions" in result.problems


def test_readiness_requires_workbench_row_for_the_release_version(pg_factory) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        session.delete(session.query(StockWorkbenchSnapshot).one())
    result = SnapshotReadinessChecker(factory).check(("600519.SH",))
    assert result.ready is False
    assert "missing workbench snapshot: 600519.SH@v1" in result.problems


def test_readiness_rejects_empty_payload_and_missing_page_snapshot(pg_factory) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        session.get(PublishedSnapshot, "macro-insights").payload = {}
        page = session.query(PageSnapshot).filter_by(product="portfolio").one()
        session.delete(page)
    result = SnapshotReadinessChecker(factory).check(("600519.SH",))
    assert result.ready is False
    assert "empty published payload: macro-insights" in result.problems
    assert "missing page snapshot: portfolio@v1" in result.problems


def test_readiness_rejects_nonempty_payload_that_violates_product_dto(pg_factory) -> None:
    factory = pg_factory
    _seed_ready_release(factory, ("600519.SH",))
    with factory.begin() as session:
        session.get(PublishedSnapshot, "market-overview").payload = {
            "unexpected": "value"
        }

    result = SnapshotReadinessChecker(factory).check(("600519.SH",))

    assert result.ready is False
    assert any(
        problem.startswith("invalid published payload: market-overview")
        for problem in result.problems
    )


def test_runner_skips_complete_release_without_commands() -> None:
    commands = RecordingCommands()
    runner = BootstrapRunner(
        SequenceChecker(ReadinessResult(True, "v1", ())), commands,
    )
    report = runner.run(_options())
    assert report.status == "skipped"
    assert report.version == "v1"
    assert commands.calls == []


def test_runner_executes_backfill_publish_and_final_check() -> None:
    commands = RecordingCommands(0, 0)
    runner = BootstrapRunner(
        SequenceChecker(
            ReadinessResult(False, None, ("missing published product: portfolio",)),
            ReadinessResult(True, "v2", ()),
        ),
        commands,
    )
    report = runner.run(_options())
    assert report.status == "completed"
    assert report.version == "v2"
    assert [call[0] for call in commands.calls] == ["backfill", "build-and-publish"]


def test_runner_stops_when_backfill_fails() -> None:
    commands = RecordingCommands(4)
    runner = BootstrapRunner(
        SequenceChecker(ReadinessResult(False, None, ("not ready",))), commands,
    )
    with pytest.raises(BootstrapCommandError, match="backfill failed with exit code 4"):
        runner.run(_options())
    assert len(commands.calls) == 1


def test_runner_propagates_publish_and_final_check_failures() -> None:
    publish_commands = RecordingCommands(0, 4)
    publish_runner = BootstrapRunner(
        SequenceChecker(ReadinessResult(False, None, ("not ready",))),
        publish_commands,
    )
    with pytest.raises(
        BootstrapCommandError,
        match="build-and-publish failed with exit code 4",
    ):
        publish_runner.run(_options())

    final_runner = BootstrapRunner(
        SequenceChecker(
            ReadinessResult(False, None, ("not ready",)),
            ReadinessResult(False, None, ("missing page snapshot: portfolio@v1",)),
        ),
        RecordingCommands(0, 0),
    )
    with pytest.raises(BootstrapCommandError, match="bootstrap final check failed"):
        final_runner.run(_options())


def test_runner_force_ignores_initial_readiness() -> None:
    commands = RecordingCommands(0, 0)
    runner = BootstrapRunner(
        SequenceChecker(
            ReadinessResult(True, "v1", ()),
            ReadinessResult(True, "v2", ()),
        ),
        commands,
    )
    report = runner.run(_options(force=True))
    assert report.status == "completed"
    assert len(commands.calls) == 2


def test_main_rejects_missing_token_without_leaking_database_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.bootstrap.Settings.from_env",
        classmethod(lambda cls: Settings(
            _env_file=None,
            data_backend="database",
            database_url="postgresql+psycopg://user:super-secret@db:5432/app",
            tushare_token=None,
        )),
    )
    assert main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "error"
    assert "DANG_TUSHARE_TOKEN" in error["message"]
    assert "super-secret" not in json.dumps(error)


def test_main_settings_validation_error_uses_safe_message_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_values = {
        "token": "fake-token-value-for-redaction-test",
        "write_url": (
            "postgresql+psycopg://writer:fake-write-password@db:5432/app"
        ),
        "read_url": (
            "postgresql+psycopg://reader:fake-read-password@replica:5432/app"
        ),
        "write_password": "fake-write-password",
        "read_password": "fake-read-password",
    }
    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", secret_values["token"])
    monkeypatch.setenv("DANG_DATABASE_URL", secret_values["write_url"])
    monkeypatch.setenv("DANG_DATABASE_READ_URL", secret_values["read_url"])
    monkeypatch.setenv("DANG_BOOTSTRAP_FORCE", "not-a-valid-boolean")

    assert main() == 1

    error_output = capsys.readouterr().err
    error = json.loads(error_output)
    leaked_labels = [
        label for label, value in secret_values.items() if value in error_output
    ]
    assert leaked_labels == []
    assert error == {
        "status": "error",
        "errorType": "ValidationError",
        "message": "bootstrap configuration is invalid",
    }


def test_redact_removes_token_password_and_full_url() -> None:
    settings = Settings(
        _env_file=None,
        data_backend="database",
        database_url="postgresql+psycopg://user:super-secret@db:5432/app",
        tushare_token="token-secret",
    )
    message = (
        "token-secret failed at "
        "postgresql+psycopg://user:super-secret@db:5432/app"
    )
    sanitized = _redact(message, settings)
    assert sanitized == "*** failed at ***"


def test_redact_removes_read_database_password_and_full_url() -> None:
    settings = Settings(
        _env_file=None,
        data_backend="database",
        database_url="postgresql+psycopg://writer:write-secret@db:5432/app",
        database_read_url=(
            "postgresql+psycopg://reader:read-secret@replica:5432/app"
        ),
        tushare_token="token-secret",
    )
    message = (
        "read-secret failed at "
        "postgresql+psycopg://reader:read-secret@replica:5432/app"
    )

    assert _redact(message, settings) == "*** failed at ***"
