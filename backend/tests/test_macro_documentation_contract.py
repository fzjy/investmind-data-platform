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


def test_macro_documentation_covers_cross_platform_commands_and_scope() -> None:
    root = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    backend = (PROJECT_ROOT / "backend/README.md").read_text(encoding="utf-8")
    mac = root.split("## 4. 如何运行项目-mac系统", 1)[1].split("## 5.", 1)[0]
    windows = root.split("## 5. 如何运行项目-windows系统", 1)[1].split("## 6.", 1)[0]
    for chapter, command in ((mac, "npm"), (windows, "npm.cmd")):
        # 单组采集已集中进阶手册；日常启动不能夹带真实smoke/强制历史采集。
        assert "(backend/README.md)" in chapter
        assert f"{command} run backend:cli -- smoke-macro --provider fred" in backend
        assert f"{command} run backend:cli -- sync-macro --help" in backend
        assert f"{command} run dev:local" in chapter
        assert f"{command} run backend:update -- --dry-run" in chapter
        assert f"{command} run backend:update -- --publish" in chapter
        assert "smoke-macro --provider fred" not in chapter
        assert "--as-of" in backend and "--force" in backend
    development = (PROJECT_ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "新闻采集" in development and "未完成" in development
    assert "rebuild-external-marts --product macro" in backend


def test_macro_example_exposes_documented_defaults_without_credentials() -> None:
    env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    entries = dict(line.split("=", 1) for line in env.splitlines() if line and not line.startswith("#"))
    for name, value in MACRO_DEFAULTS.items():
        assert entries.get(name) == value
    assert entries.get("DANG_FRED_API_KEY") == ""
    assert entries["DANG_TUSHARE_TOKEN"] == ""


def test_macro_docs_disclose_source_gaps_and_verification_boundaries() -> None:
    for relative in ("DEVELOPMENT.md", "backend/README.md"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for required in (
            FRED_NOTICE, "https://fred.stlouisfed.org/docs/api/terms_of_use.html",
            "cn.cpi.core_yoy", "cn.rate.dr007", "20", "representative-50",
            "受控", "真实 smoke", "未执行", "partial", "stale", "fallback",
            "backend/config/macro-series.json",
        ):
            assert required in text, (relative, required)


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
    compose_files = ("docker-compose.backend.yml", "docker-compose.yml", "docker-compose.fullstack.yml")
    for name in compose_files:
        shutil.copy2(PROJECT_ROOT / name, tmp_path / name)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / "config/frontend-container.env", config_dir)
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
         *[arg for name in compose_files for arg in ("-f", name)],
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


def test_eslint_ignores_plan_scratch_but_checks_application_source() -> None:
    # Exercise ESLint's effective ignores, not just text in its config.
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         'import { ESLint } from "eslint"; const lint = new ESLint(); '
         'console.log(JSON.stringify(await Promise.all(['
         'lint.isPathIgnored(".superpowers/sdd/2026-09-20-macro-data-pipeline/scratch/probe.js"),'
         'lint.isPathIgnored("src/app/page.tsx")])));'],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False]
