import json
import pytest

from typer.testing import CliRunner

from app.cli.main import app


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.delenv("DANG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DANG_DATABASE_READ_URL", raising=False)


def test_actual_cli_records_readonly_status_without_corrupting_report():
    result = CliRunner().invoke(app, ["sync-status"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert events and events[-1]["command"] == "sync-status"
    assert events[-1]["durationMs"] >= 0
    assert events[-1]["outcome"] == "failed"
    assert payload["error"]["code"] == "SYNC_STATUS_UNAVAILABLE"


@pytest.mark.parametrize("arguments,exit_code", [(["sync-daily", "--business-date", "bad"], 2),
                                                (["sync-nbs"], 3), (["catchup-macro"], 3)])
def test_actual_sync_failure_has_safety_alert_and_original_exit_code(arguments, exit_code):
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == exit_code
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(item["event"] == "safety_alert" and item["command"] == arguments[0] for item in events)
