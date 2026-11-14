from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    "docker-compose.backend.yml",
    "docker-compose.yml",
    "docker-compose.fullstack.yml",
)
FORBIDDEN_FRONTEND_ENV = {
    "DANG_DATABASE_URL",
    "DANG_DATABASE_READ_URL",
    "DANG_TUSHARE_TOKEN",
    "DANG_POSTGRES_PASSWORD",
}


def _controlled_compose_environment(tmp_path: Path) -> dict[str, str]:
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text("{}\n", encoding="utf-8")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("DANG_", "COMPOSE_"))
        and name.upper()
        not in {
            "BACKEND_INTERNAL_API_BASE_URL",
            "FRONTEND_BACKEND_INTERNAL_API_BASE_URL",
            "NEXT_PUBLIC_API_BASE_URL",
        }
    }
    environment["DOCKER_CONFIG"] = str(docker_config)
    return environment


def _render_fullstack_config(tmp_path: Path) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    for name in COMPOSE_FILES:
        shutil.copy2(PROJECT_ROOT / name, tmp_path / name)
    config_source = PROJECT_ROOT / "config" / "frontend-container.env"
    config_target = tmp_path / "config" / "frontend-container.env"
    config_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_source, config_target)
    safe_env = tmp_path / ".env"
    safe_env.write_text(
        "DANG_TUSHARE_TOKEN=contract-token\n"
        "DANG_POSTGRES_PASSWORD=contract-password\n"
        "DANG_BOOTSTRAP_SYMBOLS=600519.SH\n"
        "NEXT_PUBLIC_API_BASE_URL=https://browser.example.invalid/api\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "smartparse-fullstack-contract",
            "--env-file",
            str(safe_env),
            *[
                argument
                for name in COMPOSE_FILES
                for argument in ("-f", name)
            ],
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=_controlled_compose_environment(tmp_path),
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


def _render_frontend_only_config(
    tmp_path: Path, *, public_api_base_url: str | None = None
) -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    shutil.copy2(PROJECT_ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    config_source = PROJECT_ROOT / "config" / "frontend-container.env"
    config_target = tmp_path / "config" / "frontend-container.env"
    config_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_source, config_target)
    env_example = PROJECT_ROOT / ".env.example"
    env_text = env_example.read_text(encoding="utf-8")
    if public_api_base_url is not None:
        env_text = env_text.replace(
            "NEXT_PUBLIC_API_BASE_URL=\n",
            f"NEXT_PUBLIC_API_BASE_URL={public_api_base_url}\n",
        )
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "smartparse-frontend-only-contract",
            "--env-file",
            str(tmp_path / ".env"),
            "-f",
            "docker-compose.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=_controlled_compose_environment(tmp_path),
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


def test_dockerignore_excludes_root_environment_variants() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env*" in patterns
    assert "config/frontend-container.env" not in patterns


def test_dockerignore_excludes_generated_pytest_caches() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "**/.pytest_cache" in patterns


def test_gitignore_excludes_all_local_dotenv_variants_except_template() -> None:
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull

    for path in (
        ".env",
        ".env.local",
        ".env.development",
        ".env.development.local",
        ".env.test",
        ".env.production",
        ".env.production.local",
    ):
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.excludesFile=",
                "check-ignore",
                "--no-index",
                "--quiet",
                "--",
                path,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, f"{path} was not ignored: {result.stderr}"

    template_result = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=",
            "check-ignore",
            "--no-index",
            "--quiet",
            "--",
            ".env.example",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert template_result.returncode == 1, template_result.stderr


def test_fullstack_compose_ignores_inherited_secret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", "host-only-contract-token")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "host-only-contract-project")

    services = _render_fullstack_config(tmp_path)["services"]

    assert services["bootstrap"]["environment"]["DANG_TUSHARE_TOKEN"] == "contract-token"


def test_fullstack_compose_connects_both_frontends_to_healthy_api(tmp_path: Path) -> None:
    services = _render_fullstack_config(tmp_path)["services"]
    assert services["app"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["dev"]["depends_on"]["api"]["condition"] == "service_healthy"
    for name in ("app", "dev"):
        assert services[name]["environment"]["BACKEND_INTERNAL_API_BASE_URL"] == "http://api:8000"
        assert "default" in services[name]["networks"]
        assert "container_name" not in services[name]


def test_frontend_healthchecks_match_exact_node_fetch_command(tmp_path: Path) -> None:
    services = _render_fullstack_config(tmp_path)["services"]
    expected = [
        "CMD",
        "node",
        "-e",
        "fetch('http://localhost:3000/').then((response) => process.exit(response.ok ? 0 : 1)).catch(() => process.exit(1))",
    ]

    for name in ("app", "dev"):
        assert services[name]["healthcheck"]["test"] == expected


def test_next_config_allows_windows_loopback_dev_origin() -> None:
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            (
                "import { pathToFileURL } from 'node:url';"
                "const module = await import(pathToFileURL(process.argv[1]).href);"
                "process.stdout.write(JSON.stringify(module.default));"
            ),
            str(PROJECT_ROOT / "next.config.ts"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    next_config = json.loads(result.stdout)
    assert next_config.get("allowedDevOrigins") == ["127.0.0.1"]


def test_fullstack_compose_keeps_backend_secrets_out_of_frontends(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text(
        "DANG_TUSHARE_TOKEN=frontend-mounted-secret\n"
        "DANG_POSTGRES_PASSWORD=frontend-mounted-password\n",
        encoding="utf-8",
    )
    services = _render_fullstack_config(tmp_path)["services"]
    for name in ("app", "dev"):
        environment = services[name].get("environment", {})
        assert FORBIDDEN_FRONTEND_ENV.isdisjoint(environment)
    expected_masks = [
        {
            "type": "bind",
            "source": str((tmp_path / "config" / "frontend-container.env").resolve()),
            "target": target,
            "read_only": True,
        }
        for target in (
            "/app/.env",
            "/app/.env.local",
            "/app/.env.development",
            "/app/.env.development.local",
        )
    ]
    assert [
        {
            "type": volume["type"],
            "source": volume["source"],
            "target": volume["target"],
            "read_only": volume["read_only"],
        }
        for volume in services["dev"]["volumes"][-4:]
    ] == expected_masks


def test_frontend_only_compose_uses_host_backend_with_env_example(tmp_path: Path) -> None:
    services = _render_frontend_only_config(tmp_path)["services"]
    for name in ("app", "dev"):
        assert (
            services[name]["environment"]["BACKEND_INTERNAL_API_BASE_URL"]
            == "http://host.docker.internal:8000"
        )
        assert "api" not in services[name].get("depends_on", {})


def test_frontend_only_compose_wires_public_browser_override(tmp_path: Path) -> None:
    public_api_base_url = "https://browser.example.invalid/api"
    services = _render_frontend_only_config(
        tmp_path, public_api_base_url=public_api_base_url
    )["services"]

    assert (
        services["app"]["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"]
        == public_api_base_url
    )
    assert "NEXT_PUBLIC_API_BASE_URL" not in services["app"].get("environment", {})
    assert (
        services["dev"]["environment"]["NEXT_PUBLIC_API_BASE_URL"]
        == public_api_base_url
    )


def test_fullstack_compose_forces_same_origin_browser_proxy(tmp_path: Path) -> None:
    services = _render_fullstack_config(tmp_path)["services"]

    assert services["app"]["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"] == ""
    assert "NEXT_PUBLIC_API_BASE_URL" not in services["app"].get("environment", {})
    assert services["dev"]["environment"]["NEXT_PUBLIC_API_BASE_URL"] == ""


def test_production_dockerfile_exposes_public_override_only_during_build() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    _, dependencies_and_later = dockerfile.split(
        "FROM node:${NODE_VERSION} AS dependencies", maxsplit=1
    )
    dependencies, builder_and_later = dependencies_and_later.split(
        "FROM node:${NODE_VERSION} AS builder", maxsplit=1
    )
    builder, runner = builder_and_later.split(
        "FROM node:${NODE_VERSION} AS runner", maxsplit=1
    )
    build_arg = "ARG NEXT_PUBLIC_API_BASE_URL="
    build_env = "ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}"

    assert "NEXT_PUBLIC_API_BASE_URL" not in dependencies
    assert build_arg in builder
    assert build_env in builder
    assert builder.index(build_arg) < builder.index("npm run build")
    assert builder.index(build_env) < builder.index("npm run build")
    assert "NEXT_PUBLIC_API_BASE_URL" not in runner


def test_package_scripts_expose_cross_platform_fullstack_commands() -> None:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]
    common = "-f docker-compose.backend.yml -f docker-compose.yml -f docker-compose.fullstack.yml"
    assert common in scripts["compose:database:app"]
    assert scripts["compose:database:app"].endswith("up --build --wait api app")
    assert common in scripts["compose:database:dev"]
    assert scripts["compose:database:dev"].endswith("up --build --wait api dev")
    assert common in scripts["compose:database:down"]
    assert scripts["compose:database:down"].endswith("down")
    assert "-v" not in scripts["compose:database:down"]
    assert scripts.get("test:market") == (
        "node --no-warnings --test src/modules/market/*.test.ts"
    )
    assert scripts.get("test:search") == (
        "node --no-warnings --test src/modules/search/*.test.ts"
    )
    assert scripts.get("test:portfolio") == (
        "node --no-warnings --test src/modules/portfolio/*.test.ts"
    )
    assert scripts["test"].split(" && ") == [
        "npm run test:python-runner",
        "npm run test:local-runtime",
        "npm run test:stocks",
        "npm run test:market",
        "npm run test:macro",
        "npm run test:portfolio",
        "npm run test:frontend-data",
        "npm run test:search",
        "npm run test:backend",
        "npm run test:architecture",
    ]


def test_readme_documents_mac_and_windows_fullstack_commands() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    mac_instructions, windows_instructions = readme.split(
        "## 5. 如何运行项目-windows系统", maxsplit=1
    )
    assert "npm run compose:database:app" in readme
    assert "npm run compose:database:dev" in readme
    assert "npm run compose:database:down" in readme
    assert "npm.cmd run compose:database:app" in readme
    assert "npm.cmd run compose:database:dev" in readme
    assert "npm.cmd run compose:database:down" in readme
    assert "http://api:8000" in readme
    assert "/backend-api" in readme
    for instructions in (mac_instructions, windows_instructions):
        assert "BACKEND_INTERNAL_API_BASE_URL" in instructions
        assert "FRONTEND_BACKEND_INTERNAL_API_BASE_URL" in instructions
        assert "http://host.docker.internal:8000" in instructions


def test_env_example_defaults_browser_requests_to_same_origin() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "BACKEND_INTERNAL_API_BASE_URL=http://127.0.0.1:8000" in env_example
    assert (
        "FRONTEND_BACKEND_INTERNAL_API_BASE_URL=http://host.docker.internal:8000"
        in env_example
    )
    assert "NEXT_PUBLIC_API_BASE_URL=\n" in env_example
