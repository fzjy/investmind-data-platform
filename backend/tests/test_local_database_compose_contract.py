"""仅解析假配置，不启动Docker或连接业务数据库。"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("port", [None, "15432"])
def test_local_database_override_exposes_only_loopback_and_keeps_volume(tmp_path, port):
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    for name in ("docker-compose.backend.yml", "docker-compose.local-db.yml"):
        shutil.copy2(ROOT / name, tmp_path / name)
    env_file = tmp_path / "fake.env"
    env_file.write_text("DANG_POSTGRES_PASSWORD=contract-only\n" +
                        (f"DANG_POSTGRES_PORT={port}\n" if port else ""), encoding="utf-8")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text("{}", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("DANG_", "COMPOSE_"))}
    environment["DOCKER_CONFIG"] = str(docker_config)
    result = subprocess.run([
        "docker", "compose", "--project-name", "local-db-contract", "--env-file", str(env_file),
        "-f", "docker-compose.backend.yml", "-f", "docker-compose.local-db.yml",
        "config", "--format", "json",
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)
    missing_plugin = any(marker in result.stderr.lower() for marker in (
        "'compose' is not a docker command", '"compose" is not a docker command',
        "unknown command: compose",
    ))
    if result.returncode != 0 and missing_plugin:
        pytest.skip("Docker Compose plugin is not available")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    database = config["services"]["postgres"]
    assert database["ports"] == [{"mode": "ingress", "host_ip": "127.0.0.1",
                                  "target": 5432, "published": port or "5432", "protocol": "tcp"}]
    assert database["volumes"][0]["source"] == "danginvest-postgres-data"
    assert "depends_on" not in database  # 单启postgres不能顺带启动采集或业务服务。
    assert database["environment"]["POSTGRES_PASSWORD"] == "contract-only"
