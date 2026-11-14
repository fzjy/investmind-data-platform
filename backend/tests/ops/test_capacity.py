"""容量命令的安全边界及根配置消费契约。"""
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

import app.config as config_module
from app.config import Settings


def capacity():
    assert importlib.util.find_spec("app.ops.capacity") is not None, "缺少容量验证实现"
    return importlib.import_module("app.ops.capacity")


def cli():
    assert importlib.util.find_spec("app.cli.capacity") is not None, "缺少容量 CLI"
    module = importlib.import_module("app.cli.capacity")
    app = typer.Typer()
    module.register_capacity_commands(app)
    return module, app


def test_root_dotenv_controls_actual_synthetic_rows(tmp_path, monkeypatch):
    module = capacity()
    dotenv = tmp_path / ".env"
    dotenv.write_text("DANG_CAPACITY_STOCK_COUNT=2\nDANG_CAPACITY_TRADING_DAYS=3\nDANG_CAPACITY_BATCH_SIZE=2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", dotenv)
    settings = Settings.from_env()
    # 配置字段缺失必须在生成之前失败，避免 fallback 意外生成全规模。
    assert getattr(settings, "capacity_stock_count", None) == 2
    options = module.CapacityOptions.from_settings(settings)
    batches = list(module.synthetic_price_batches(options))
    assert [len(batch) for batch in batches] == [2, 2, 2]
    assert len({(row["ts_code"], row["trade_date"]) for batch in batches for row in batch}) == 6


def test_synthetic_batches_are_bounded_distinct_and_weekdays():
    module = capacity()
    options = module.CapacityOptions(stock_count=2, trading_days=3, batch_size=4)
    batches = list(module.synthetic_price_batches(options))
    assert [len(batch) for batch in batches] == [4, 2]
    assert {row["trade_date"].weekday() for batch in batches for row in batch} <= {0, 1, 2, 3, 4}
    assert {row["data"]["source"] for batch in batches for row in batch} == {"synthetic"}


@pytest.mark.parametrize("fields", [{"stock_count": 0}, {"trading_days": 0}, {"batch_size": 0}, {"api_samples": 0}, {"timeout_seconds": 0}, {"build_budget_seconds": -1}])
def test_invalid_size_or_budget_fails_before_container(fields):
    with pytest.raises(ValueError):
        capacity().CapacityOptions(**fields)


def test_budget_failure_and_unconfigured_recommendations_are_distinct():
    module = capacity()
    report = module.assess_budgets(301, 501, module.CapacityOptions())
    assert report["build"]["passed"] is False
    assert report["api"]["passed"] is False
    assert report["build"]["configured"] is False
    configured = module.assess_budgets(1, 1, module.CapacityOptions(build_budget_seconds=0.5, api_budget_ms=0.5))
    assert configured["build"]["configured"] is True
    assert configured["api"]["passed"] is False


def test_status_rejects_non_postgres_without_connecting():
    with pytest.raises(capacity().CapacityError, match="capacity_postgres_required"):
        capacity().capacity_status(SimpleNamespace(database_read_url="sqlite:///unused", database_url=None))


@pytest.mark.parametrize("command", ["capacity-status", "capacity-benchmark"])
def test_cli_redacts_configuration_failures(command, monkeypatch):
    module, app = cli()
    def fail():
        raise ValueError("postgresql://u:secret-canary@business/db token=canary")
    monkeypatch.setattr(module.Settings, "from_env", fail)
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reasonCode"] == "invalid_capacity_configuration"
    assert "canary" not in result.output


@pytest.mark.parametrize("command", ["capacity-status", "capacity-benchmark"])
def test_cli_never_accepts_target_url(command):
    _, app = cli()
    result = CliRunner().invoke(app, [command, "--target-url", "postgresql://u:secret-canary@business/db"])
    assert result.exit_code == 2
    assert "secret-canary" not in result.output


def test_cli_budget_exceedance_is_nonzero(monkeypatch):
    module, app = cli()
    monkeypatch.setattr(module.Settings, "from_env", lambda: Settings(_env_file=None))
    monkeypatch.setattr(module, "capacity_benchmark", lambda *args, **kwargs: {"status": "budget_exceeded", "source": "synthetic", "exitCode": 4})
    result = CliRunner().invoke(app, ["capacity-benchmark", "--stocks", "2", "--days", "3"])
    assert result.exit_code == 4
    assert json.loads(result.stdout)["source"] == "synthetic"


@pytest.mark.parametrize("configured, expected", [(5, 5), (1800, 30)])
def test_status_cli_root_defaults_and_safe_timeout_cap(tmp_path, monkeypatch, configured, expected):
    module, app = cli()
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DANG_CAPACITY_API_SAMPLES=1\nDANG_CAPACITY_TIMEOUT_SECONDS={configured}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", dotenv)
    def boundary(settings, *, timeout_seconds, api_samples):
        return {"status": "completed", "timeoutSeconds": timeout_seconds, "apiSamples": api_samples, "exitCode": 0}
    monkeypatch.setattr(module, "capacity_status", boundary)
    result = CliRunner().invoke(app, ["capacity-status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["timeoutSeconds"] == expected
    assert json.loads(result.stdout)["apiSamples"] == 1
    explicit = CliRunner().invoke(app, ["capacity-status", "--api-samples", "2", "--timeout-seconds", "3"])
    assert json.loads(explicit.stdout)["apiSamples"] == 2
    assert json.loads(explicit.stdout)["timeoutSeconds"] == 3


def test_capacity_emit_links_actual_report_to_command_event(monkeypatch):
    from app.ops.observability import install_cli_observability
    module, app = cli()
    settings = Settings(_env_file=None)
    monkeypatch.setattr(module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(module, "capacity_benchmark", lambda *args, **kwargs: {"status": "budget_exceeded", "scale": {"dailyPriceRows": 6}, "budgets": {"build": {"passed": False}}, "exitCode": 4})
    install_cli_observability(app, settings_loader=lambda: settings)
    result = CliRunner().invoke(app, ["capacity-benchmark", "--stocks", "2", "--days", "3"])
    assert result.exit_code == 4
    event = next(json.loads(line) for line in result.stderr.splitlines() if '"event": "cli_command"' in line)
    assert event["state"] == "budget_exceeded"
    assert "rowCount" not in event
    assert event["scale"] == {"dailyPriceRows": 6}
    assert event["budgets"]["build"]["passed"] is False
    assert json.loads(result.stdout)["scale"] == {"dailyPriceRows": 6}
