from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.jobs.scheduler_logging import SchedulerJsonLogger


def test_structured_logger_emits_one_redacted_json_line(capsys) -> None:
    logger = SchedulerJsonLogger(
        "scheduler-1",
        sanitize=lambda value: value.replace("secret", "***"),
        clock=lambda: datetime(2026, 9, 8, 10, 11, 12, tzinfo=UTC),
    )

    logger.emit(
        "scheduler.daily.failed",
        "error",
        message="token secret",
        business_date="2026-09-08",
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "timestamp": "2026-09-08T10:11:12Z",
        "level": "error",
        "event": "scheduler.daily.failed",
        "schedulerInstanceId": "scheduler-1",
        "message": "token ***",
        "businessDate": "2026-09-08",
    }
    assert captured.err == ""
    assert "secret" not in lines[0]


def test_structured_logger_sanitizes_nested_values_and_bounds_messages(capsys) -> None:
    database_url = "postgresql+psycopg://user:password@db.local/app"
    logger = SchedulerJsonLogger(
        "scheduler-1",
        sanitize=lambda value: value.replace(database_url, "***"),
        clock=lambda: datetime(2026, 9, 8, 18, 11, 12, tzinfo=UTC),
    )

    logger.emit(
        "scheduler.freshness.failed",
        "error",
        error_code="DATABASE_ERROR",
        message=f"{database_url} " + ("x" * 1000),
        details={"source": database_url},
    )

    line = capsys.readouterr().out
    payload = json.loads(line)
    assert database_url not in line
    assert payload["details"] == {"source": "***"}
    assert len(payload["message"]) <= 500


def test_startup_logger_allows_null_instance_identity(capsys) -> None:
    logger = SchedulerJsonLogger(
        None,
        clock=lambda: datetime(2026, 9, 8, 18, 11, 12, tzinfo=UTC),
    )

    logger.emit(
        "scheduler.startup.failed",
        "error",
        error_code="SCHEDULER_STARTUP_FAILED",
        message="scheduler startup failed",
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["schedulerInstanceId"] is None


def test_default_threshold_suppresses_debug_but_keeps_operational_levels(capsys) -> None:
    logger = SchedulerJsonLogger("scheduler-1")
    for level in ("debug", "info", "warning", "error"):
        logger.emit("scheduler.heartbeat", level)
    assert [json.loads(line)["level"] for line in capsys.readouterr().out.splitlines()] == [
        "info", "warning", "error",
    ]


def test_explicit_debug_threshold_emits_heartbeat(capsys) -> None:
    logger = SchedulerJsonLogger("scheduler-1", min_level="debug")
    logger.emit("scheduler.heartbeat", "debug")
    assert json.loads(capsys.readouterr().out)["level"] == "debug"


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="level"):
        SchedulerJsonLogger("scheduler-1", min_level="invalid")


def test_invalid_event_level_does_not_write_or_break_later_logs(capsys) -> None:
    logger = SchedulerJsonLogger("scheduler-1")
    logger.emit("scheduler.started", "info")
    with pytest.raises(ValueError, match="level"):
        logger.emit("scheduler.heartbeat", "invalid")
    logger.emit("scheduler.stopping", "info")
    assert [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()] == [
        "scheduler.started", "scheduler.stopping",
    ]
