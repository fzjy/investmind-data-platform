"""独立底座镜像与本地配置隔离，服务组合在 test_backend_compose_contract.py 验收。"""
import os
from pathlib import Path
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_dockerignore_excludes_root_environment_variants() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env*" in patterns
    assert {"backend/backups", ".secrets", "**/*.dump"} <= patterns


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
