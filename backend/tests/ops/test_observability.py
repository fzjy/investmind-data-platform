"""真实 CLI 边界：漏事件、串报告、改变退出结果或泄密都必须使测试失败。"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import logging
from typing import Annotated

import pytest
import typer
from pydantic import SecretStr
from typer.testing import CliRunner

from app.config import Settings


def observability():
    assert importlib.util.find_spec("app.ops.observability") is not None, "缺少 CLI 可观测性模块"
    return importlib.import_module("app.ops.observability")


def isolated_settings() -> Settings:
    return Settings(_env_file=None, database_url=None, database_read_url=None)


def events(result) -> list[dict[str, object]]:
    return [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]


def command_event(result) -> dict[str, object]:
    found = [event for event in events(result) if event["event"] == "cli_command"]
    assert len(found) == 1
    return found[0]


def small_app():
    module = observability()
    app = typer.Typer(add_completion=False)

    @app.command("run")
    def run(mode: Annotated[str, typer.Option("--mode")] = "success") -> None:
        if mode == "exception":
            raise RuntimeError("provider unavailable")
        if mode == "no-report":
            typer.echo("plain output")
            return
        module.emit_report({
            "status": "completed" if mode in ("success", "zero-exit") else "partial",
            "jobRunId": "job-17", "batchIds": ["batch-a", "batch-b"],
            "rowCount": 12, "rejectedCount": 0, "releaseVersion": "release-4",
        })
        if mode == "nonzero":
            raise typer.Exit(code=4)
        if mode == "zero-exit":
            raise typer.Exit(code=0)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    return app


@pytest.mark.parametrize("mode,code,state", [
    ("success", 0, "completed"), ("nonzero", 4, "partial"),
    ("zero-exit", 0, "completed"), ("exception", 1, "failed"),
])
def test_actual_command_preserves_exit_and_emits_outcome(mode, code, state) -> None:
    result = CliRunner().invoke(small_app(), ["--mode", mode])
    assert result.exit_code == code
    event = command_event(result)
    assert event["command"] == "run"
    assert event["state"] == state
    assert event["exitCode"] == code
    assert event["outcome"] == ("succeeded" if code == 0 else "failed")
    assert isinstance(event["durationMs"], (int, float))
    assert event["durationMs"] >= 0
    if mode == "exception":
        assert isinstance(result.exception, RuntimeError)
        assert event["errorType"] == "RuntimeError"
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["jobRunId"] == "job-17"
        assert event["jobRunId"] == "job-17"
        assert event["batchIds"] == ["batch-a", "batch-b"]
        assert event["rowCount"] == 12
        assert event["rejectedCount"] == 0
        assert event["releaseVersion"] == "release-4"


def test_missing_report_values_are_omitted() -> None:
    result = CliRunner().invoke(small_app(), ["--mode", "no-report"])
    assert result.exit_code == 0
    assert result.stdout == "plain output\n"
    event = command_event(result)
    for key in ("jobRunId", "batchIds", "rowCount", "rejectedCount", "releaseVersion"):
        assert key not in event


def test_repeated_install_and_invocations_do_not_duplicate_or_reuse_reports() -> None:
    module = observability()
    app = small_app()
    module.install_cli_observability(app, settings_loader=isolated_settings)
    first = CliRunner().invoke(app)
    second = CliRunner().invoke(app, ["--mode", "no-report"])
    assert command_event(first)["jobRunId"] == "job-17"
    assert "jobRunId" not in command_event(second)


def test_multiple_stdout_writes_remain_unmodified() -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def progress() -> None:
        typer.echo("before")
        module.emit_report({"rowCount": 2})
        typer.echo("after")

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["before", '{"rowCount": 2}', "after"]
    assert command_event(result)["rowCount"] == 2


@pytest.mark.parametrize("report,expected", [
    ({"batches": [{"batchId": "b1", "rowCount": 3, "rejectedCount": 1},
                  {"batchId": "b2", "rowCount": 4, "rejectedCount": 2}],
      "plans": [{"rowCount": 7, "rejectedCount": 3, "batchIds": ["b1", "b2"]}],
      "rejected": 3}, {"batchIds": ["b1", "b2"], "rowCount": 7, "rejectedCount": 3}),
    ({"rowCount": 7, "rejectedCount": 0, "outcomes": [{"rowCount": 7}],
      "publicationVersion": "macro-1"}, {"rowCount": 7, "rejectedCount": 0, "publicationVersion": "macro-1"}),
    ({"rows": 5, "version": "v1", "releaseVersion": None}, {"rowCount": 5, "version": "v1"}),
    ({"metricCount": 8, "riskCount": 2, "version": "v2"}, {"version": "v2"}),
])
def test_report_shapes_do_not_double_count_or_invent_publication(report, expected) -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def report_command() -> None:
        module.emit_report(report)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 0
    event = command_event(result)
    for key, value in expected.items():
        assert event[key] == value
    if "rowCount" not in expected:
        assert "rowCount" not in event
    if report.get("releaseVersion") is None:
        assert "releaseVersion" not in event


def test_nested_commands_preserve_signature_and_command_path() -> None:
    module = observability()
    app = typer.Typer()
    group = typer.Typer()
    app.add_typer(group, name="ops")

    @group.command("publish")
    def publish(limit: Annotated[int, typer.Option("--limit")] = 2) -> None:
        module.emit_report({"rowCount": limit})

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app, ["ops", "publish", "--limit", "9"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"rowCount": 9}
    assert command_event(result)["command"] == "ops publish"


def test_safety_critical_failure_emits_local_critical_structured_alert(caplog) -> None:
    module = observability()
    app = typer.Typer()

    @app.command("build-and-publish")
    def publish() -> None:
        module.emit_report({"status": "blocked", "jobRunId": "job-blocked"})
        raise typer.Exit(code=4)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    with caplog.at_level(logging.INFO, logger=module.LOGGER_NAME):
        result = CliRunner().invoke(app)
    assert result.exit_code == 4
    assert json.loads(result.stdout)["status"] == "blocked"
    alerts = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert len(alerts) == 1
    assert alerts[0].alert["code"] == "SAFETY_CRITICAL_COMMAND_FAILED"
    assert alerts[0].structured_event["jobRunId"] == "job-blocked"
    assert len([event for event in events(result) if "alert" in event]) == 1


def test_ordinary_failure_does_not_trigger_safety_alert() -> None:
    result = CliRunner().invoke(small_app(), ["--mode", "nonzero"])
    assert result.exit_code == 4
    assert not any("alert" in event for event in events(result))


def test_resolved_file_secret_and_url_and_bearer_do_not_reach_logs(tmp_path, caplog) -> None:
    module = observability()
    secret_file = tmp_path / "token.secret"
    secret_file.write_text("resolved-secret-17", encoding="utf-8")

    def settings_loader() -> Settings:
        return Settings(_env_file=None, tushare_token=SecretStr(secret_file.read_text(encoding="utf-8")),
                        database_url="postgresql://u:db-pass@localhost/unused")

    app = typer.Typer()

    @app.command("build-and-publish")
    def fail() -> None:
        raise RuntimeError("resolved-secret-17 db-pass https://u:url-pass@host Bearer header-secret api_key=query-secret")

    module.install_cli_observability(app, settings_loader=settings_loader)
    with caplog.at_level(logging.INFO, logger=module.LOGGER_NAME):
        result = CliRunner().invoke(app)
    assert result.exit_code == 1
    rendered = result.stderr + caplog.text
    for secret in ("resolved-secret-17", "db-pass", "url-pass", "header-secret", "query-secret"):
        assert secret not in rendered
    assert "***" in rendered


def test_report_and_explicit_alert_redact_nested_headers_and_secret_keys(caplog) -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def report() -> None:
        module.emit_report({"status": "completed", "headers": {"Authorization": "Bearer private-header"},
                            "token": "private-token", "message": "https://u:private-url@host"})
        module.emit_safety_alert("QUALITY_GATE_BLOCKED", "Bearer private-alert", headers={"apiKey": "private-key"})

    module.install_cli_observability(app, settings_loader=isolated_settings)
    with caplog.at_level(logging.INFO, logger=module.LOGGER_NAME):
        result = CliRunner().invoke(app)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "completed"
    for secret in ("private-header", "private-token", "private-url", "private-alert", "private-key"):
        assert secret not in result.output + caplog.text
    assert any(record.alert["code"] == "QUALITY_GATE_BLOCKED"
               for record in caplog.records if record.levelno == logging.CRITICAL)


def test_settings_failure_does_not_mask_command_or_log_unsafe_exception() -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def safe() -> None:
        module.emit_report({"rowCount": 0})

    def bad_settings():
        raise ValueError("unknown raw-secret-value")

    module.install_cli_observability(app, settings_loader=bad_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 0
    assert command_event(result)["rowCount"] == 0
    assert "raw-secret-value" not in result.stderr
    assert command_event(result)["settingsErrorType"] == "ValueError"


def test_real_app_command_without_database_preserves_unavailable_json() -> None:
    module = observability()
    from app.cli.main import app as real_app

    app = copy.deepcopy(real_app)
    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app, ["sync-status"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["status"] == "unavailable"
    event = command_event(result)
    assert event["command"] == "sync-status"
    assert event["outcome"] == "failed"
    assert "jobRunId" not in event


def test_emit_report_outside_invocation_is_still_a_normal_json_emitter(capsys) -> None:
    observability().emit_report({"rowCount": 0})
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"rowCount": 0}
    assert captured.err == ""


@pytest.mark.parametrize("mode,code", [("success", 0), ("exception", 1)])
def test_external_logging_handler_failure_cannot_replace_command_result(mode, code) -> None:
    module = observability()
    app = small_app()

    class UnavailableCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("collector unavailable")

    logger = logging.getLogger(module.LOGGER_NAME)
    handler = UnavailableCollector()
    logger.addHandler(handler)
    try:
        result = CliRunner().invoke(app, ["--mode", mode])
    finally:
        logger.removeHandler(handler)
    assert result.exit_code == code
    if mode == "exception":
        assert isinstance(result.exception, RuntimeError)
        assert str(result.exception) == "provider unavailable"
    else:
        assert json.loads(result.stdout)["jobRunId"] == "job-17"


def test_multiple_reports_use_final_report_without_stale_identifiers() -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def run() -> None:
        module.emit_report({"jobRunId": "old-job", "version": "old-version", "rowCount": 100})
        module.emit_report({"status": "blocked", "rowCount": 0})
        raise typer.Exit(code=4)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 4
    assert len(result.stdout.splitlines()) == 2
    event = command_event(result)
    assert event["rowCount"] == 0
    assert "jobRunId" not in event
    assert "version" not in event


@pytest.mark.parametrize("report", [
    {"rowCount": None, "rejectedCount": True},
    {"batches": [{"rowCount": 3}, {"rowCount": None, "rejectedCount": 1}]},
])
def test_missing_or_invalid_counters_never_become_fabricated_totals(report) -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def run() -> None:
        module.emit_report(report)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 0
    event = command_event(result)
    assert "rowCount" not in event
    assert "rejectedCount" not in event


def test_error_report_can_be_correlated_from_stderr_without_writing_stdout() -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def run() -> None:
        module.emit_report({"status": "failed", "jobRunId": "error-job"}, err=True)
        raise typer.Exit(code=3)

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == 3
    assert result.stdout == ""
    lines = [json.loads(line) for line in result.stderr.splitlines()]
    assert lines[0] == {"status": "failed", "jobRunId": "error-job"}
    assert lines[1]["event"] == "cli_command"
    assert lines[1]["jobRunId"] == "error-job"


@pytest.mark.parametrize("error,code", [(typer.Abort(), 1), (typer.BadParameter("invalid option"), 2), (SystemExit(5), 5)])
def test_control_flow_failures_keep_actual_exit_code(error, code) -> None:
    module = observability()
    app = typer.Typer()

    @app.command()
    def run() -> None:
        raise error

    module.install_cli_observability(app, settings_loader=isolated_settings)
    result = CliRunner().invoke(app)
    assert result.exit_code == code
    assert command_event(result)["exitCode"] == code
