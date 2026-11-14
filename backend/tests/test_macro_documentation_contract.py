from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_DEFAULTS = {
    "DANG_MACRO_SYNC_ENABLED": "true",
    "DANG_MACRO_SYNC_DAY_OF_WEEK": "mon",
    "DANG_MACRO_SYNC_TIME": "09:00",
    "DANG_MACRO_SYNC_CATCHUP_ON_START": "true",
    "DANG_MACRO_HISTORY_YEARS": "10",
    "DANG_SYNC_TIMEZONE": "Asia/Shanghai",
}
FRED_NOTICE = (
    "This product uses the FRED® API but is not endorsed or certified "
    "by the Federal Reserve Bank of St. Louis."
)


def test_macro_documentation_uses_the_installed_cli() -> None:
    guide = (PROJECT_ROOT/"backend/README.md").read_text(encoding="utf-8")
    for cmd in ("sync-macro --help", "smoke-macro --provider fred", "rebuild-external-marts --product macro"):
        assert "python -m app.cli.main " + cmd in guide
    assert "--as-of" in guide and "--force" in guide


def test_macro_example_exposes_documented_defaults_without_credentials() -> None:
    env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    entries = dict(line.split("=", 1) for line in env.splitlines() if line and not line.startswith("#"))
    for name, value in MACRO_DEFAULTS.items():
        assert entries.get(name) == value
    assert entries.get("DANG_FRED_API_KEY") == ""
    assert entries["DANG_TUSHARE_TOKEN"] == ""


def test_macro_docs_disclose_source_gaps_and_verification_boundaries() -> None:
    text = (PROJECT_ROOT / "backend/README.md").read_text(encoding="utf-8")
    for required in (
        FRED_NOTICE, "https://fred.stlouisfed.org/docs/api/terms_of_use.html",
        "cn.cpi.core_yoy", "cn.rate.dr007", "20", "representative-50",
        "受控", "真实 smoke", "未执行", "partial", "stale", "fallback",
        "backend/config/macro-series.json",
    ):
        assert required in text, required


@pytest.mark.parametrize("overrides", [
    {},
    {name: "" for name in MACRO_DEFAULTS},
    {
        "DANG_MACRO_SYNC_ENABLED": "false",
        "DANG_MACRO_SYNC_DAY_OF_WEEK": "fri",
        "DANG_MACRO_SYNC_TIME": "17:45",
        "DANG_MACRO_SYNC_CATCHUP_ON_START": "false",
        "DANG_MACRO_HISTORY_YEARS": "7",
        "DANG_SYNC_TIMEZONE": "America/New_York",
    },
])
def test_compose_forwards_macro_schedule_and_scopes_fred_secret(
    tmp_path: Path, overrides: dict[str, str],
) -> None:
    # Removing a forwarded variable or leaking the key to another service must fail.
    assert shutil.which("docker") is not None, "Docker Compose is required for this contract"
    compose = "docker-compose.backend.yml"
    shutil.copy2(PROJECT_ROOT / compose, tmp_path / compose)
    fake_env = tmp_path / "fake.env"
    fake_env.write_text(
        "DANG_FRED_API_KEY=task12-fake-fred-key\n"
        "DANG_TUSHARE_TOKEN=task12-fake-token\n"
        "DANG_POSTGRES_PASSWORD=task12-fake-password\n"
        + "".join(f"{name}={value}\n" for name, value in overrides.items()),
        encoding="utf-8",
    )
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text("{}\n", encoding="utf-8")
    environment = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("DANG_", "COMPOSE_"))
        and name.upper() not in {
            "BACKEND_INTERNAL_API_BASE_URL", "FRONTEND_BACKEND_INTERNAL_API_BASE_URL",
            "NEXT_PUBLIC_API_BASE_URL",
        }
    }
    environment["DOCKER_CONFIG"] = str(docker_config)
    result = subprocess.run(
        ["docker", "compose", "--project-name", "task12-macro-contract",
         "--env-file", str(fake_env),
         "-f", compose,
         "--profile", "scheduler", "config", "--format", "json"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    expected = {name: overrides.get(name) or value for name, value in MACRO_DEFAULTS.items()}
    for service in ("scheduler", "api"):
        actual = services[service]["environment"]
        assert {name: actual.get(name) for name in expected} == expected
    assert services["scheduler"]["environment"]["DANG_FRED_API_KEY"] == "task12-fake-fred-key"
    for name, service in services.items():
        if name != "scheduler":
            assert "DANG_FRED_API_KEY" not in service.get("environment", {})
            assert "task12-fake-fred-key" not in json.dumps(service)
