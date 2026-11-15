from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _render_compose_config(
    *,
    project_name: str,
    profiles: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    profile_args = [
        argument
        for profile in profiles
        for argument in ("--profile", profile)
    ]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(PROJECT_ROOT / ".env.example"),
            "--project-name",
            project_name,
            "-f",
            "docker-compose.backend.yml",
            *profile_args,
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(environment or {})},
    )
    missing_plugin = any(
        marker in result.stderr.lower()
        for marker in (
            "'compose' is not a docker command",
            '"compose" is not a docker command',
            "unknown command: compose",
        )
    )
    if result.returncode != 0 and missing_plugin:
        pytest.skip("Docker Compose plugin is not available")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert isinstance(config, dict)
    return config


def test_backend_compose_has_bootstrap_success_gate() -> None:
    services = _render_compose_config(
        project_name="smartparse-contract",
        profiles=("scheduler",),
    )["services"]
    assert isinstance(services, dict)
    assert "bootstrap" in services
    assert services["bootstrap"]["command"] == ["python", "-m", "app.bootstrap"]
    assert services["bootstrap"]["restart"] == "no"
    assert (
        services["bootstrap"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        services["api"]["depends_on"]["bootstrap"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        services["scheduler"]["depends_on"]["bootstrap"]["condition"]
        == "service_completed_successfully"
    )
    assert services["scheduler"]["profiles"] == ["scheduler"]
    assert services["news-worker"]["profiles"] == ["scheduler"]
    assert services["news-worker"]["command"] == ["python", "-m", "app.cli.main", "watch-news"]
    assert "DANG_DATABASE_URL" in services["api"]["environment"]
    assert "DANG_DATABASE_READ_URL" in services["api"]["environment"]
    for name in ("postgres", "bootstrap", "api", "scheduler"):
        assert "container_name" not in services[name]
    for name in (
        "DANG_TUSHARE_TOKEN",
        "DANG_TUSHARE_MODE",
        "DANG_SYNC_TIMEZONE",
        "DANG_BOOTSTRAP_SYMBOLS",
        "DANG_BOOTSTRAP_AS_OF",
        "DANG_BOOTSTRAP_FORCE",
    ):
        assert name in services["bootstrap"]["environment"]


def test_backend_compose_excludes_scheduler_without_profile() -> None:
    services = _render_compose_config(
        project_name="smartparse-default-services",
    )["services"]

    assert isinstance(services, dict)
    assert "scheduler" not in services


def test_backend_compose_scheduler_profile_command_and_default_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_environment = {
        "DANG_SYNC_HOUR": "19",
        "DANG_SYNC_MINUTE": "0",
        "DANG_SYNC_UNIVERSE": "representative-50",
        "DANG_SYNC_UNIVERSE_FILE": "",
        "DANG_SYNC_FAILURE_ALERT_THRESHOLD": "3",
        "DANG_SYNC_CATCHUP_MAX_DAYS": "10",
        "DANG_SCHEDULER_HEARTBEAT_SECONDS": "60",
        "DANG_SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS": "180",
        "DANG_SYNC_FRESHNESS_INTERVAL_SECONDS": "300",
    }
    for name in expected_environment:
        monkeypatch.setenv(name, "")

    services = _render_compose_config(
        project_name="smartparse-scheduler-contract",
        profiles=("scheduler",),
    )["services"]

    assert isinstance(services, dict)
    scheduler = services["scheduler"]
    assert scheduler["profiles"] == ["scheduler"]
    assert scheduler["command"] == ["python", "-m", "app.jobs.scheduler"]
    assert {
        name: scheduler["environment"].get(name)
        for name in expected_environment
    } == expected_environment


def test_backend_compose_scheduler_forwards_explicit_environment() -> None:
    expected_environment = {
        "DANG_SYNC_HOUR": "7",
        "DANG_SYNC_MINUTE": "45",
        "DANG_SYNC_UNIVERSE": "custom",
        "DANG_SYNC_UNIVERSE_FILE": "/imports/custom-universe.json",
        "DANG_SYNC_FAILURE_ALERT_THRESHOLD": "5",
        "DANG_SYNC_CATCHUP_MAX_DAYS": "4",
        "DANG_SCHEDULER_HEARTBEAT_SECONDS": "15",
        "DANG_SCHEDULER_HEARTBEAT_TIMEOUT_SECONDS": "90",
        "DANG_SYNC_FRESHNESS_INTERVAL_SECONDS": "120",
    }
    services = _render_compose_config(
        project_name="smartparse-scheduler-overrides-contract",
        profiles=("scheduler",),
        environment=expected_environment,
    )["services"]

    assert isinstance(services, dict)
    scheduler = services["scheduler"]
    assert {
        name: scheduler["environment"].get(name)
        for name in expected_environment
    } == expected_environment


def test_backend_compose_isolates_named_volumes_by_project() -> None:
    first = _render_compose_config(project_name="smartparse-volume-a")
    second = _render_compose_config(project_name="smartparse-volume-b")

    first_volumes = {
        volume["name"] for volume in first["volumes"].values()
    }
    second_volumes = {
        volume["name"] for volume in second["volumes"].values()
    }
    assert first_volumes
    assert second_volumes
    assert first_volumes.isdisjoint(second_volumes)


def test_backend_compose_preserves_explicit_empty_bootstrap_symbols() -> None:
    config = _render_compose_config(
        project_name="smartparse-empty-bootstrap-symbols",
        environment={"DANG_BOOTSTRAP_SYMBOLS": ""},
    )

    services = config["services"]
    assert isinstance(services, dict)
    assert services["bootstrap"]["environment"]["DANG_BOOTSTRAP_SYMBOLS"] == ""

def test_application_containers_mount_the_same_readonly_configuration():
    services = _render_compose_config(project_name="config-mount", profiles=("scheduler",))["services"]
    for name in ("migrate", "bootstrap", "api", "news-worker", "scheduler"):
        mounts = [v for v in services[name]["volumes"] if v["target"] == "/app/config"]
        assert len(mounts) == 1
        assert mounts[0]["type"] == "bind" and mounts[0]["read_only"]
        assert Path(mounts[0]["source"]).resolve() == PROJECT_ROOT / "config"
