from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from app.config import PROJECT_ROOT
from app.ops.backup.contracts import BackupError, DatabaseInventory, PublicationInventory


def module():
    assert importlib.util.find_spec("app.ops.backup.api_probe") is not None, "isolated API probe is missing"
    return importlib.import_module("app.ops.backup.api_probe")


def test_api_probe_ignores_user_dotenv(tmp_path: Path) -> None:
    module()
    evil = tmp_path / "evil.env"
    evil.write_text("DANG_DATA_BACKEND=fixture\nDANG_DATABASE_URL=postgresql://business-canary\nDANG_TUSHARE_TOKEN=secret-canary\n", encoding="utf-8")
    script = f"""
import sys, os, json
from pathlib import Path
sys.path.insert(0, {str(PROJECT_ROOT / 'backend')!r})
import app.config as config
config.PROJECT_ENV_FILE=Path(sys.argv[1])
os.environ['DANG_DATABASE_URL']='postgresql://business-canary'
os.environ['DANG_TUSHARE_TOKEN']='secret-canary'
from app.ops.backup.config import parse_target
from app.ops.backup.api_probe import configure_worker
target=parse_target('postgresql://u:p@isolated-target/db')
settings=configure_worker(target)
print(json.dumps({{'database':settings.data_backend=='database','target': 'isolated-target' in settings.database_url,'no_token': settings.tushare_token is None,'missing_dotenv':not config.PROJECT_ENV_FILE.exists(),'no_vendor_env':'DANG_TUSHARE_TOKEN' not in os.environ}}))
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(evil)], env=os.environ, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"database": True, "target": True, "no_token": True, "missing_dotenv": True, "no_vendor_env": True}
    assert b"secret-canary" not in result.stdout


@pytest.mark.parametrize("wrong", ["fixture", "version", "count"])
def test_fixture_version_or_count_blocks_api_verification(wrong: str) -> None:
    probe = module()
    expected = DatabaseInventory(tables=(), publications=(PublicationInventory(product="screener", version="v1", row_count=3),))
    def response(request: httpx.Request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "database": "ready"})
        return httpx.Response(200, json={"meta": {"source": "fixture" if wrong == "fixture" else "database", "version": "bad" if wrong == "version" else "v1", "stale": False}, "data": {"total": 2 if wrong == "count" else 3, "items": []}})
    with httpx.Client(transport=httpx.MockTransport(response), base_url="http://testserver") as client:
        with pytest.raises(BackupError) as caught:
            probe.check_api(client, expected)
    assert caught.value.exit_code == 4


def test_degraded_failures_do_not_require_healthy_meta() -> None:
    probe = module()
    expected = DatabaseInventory(tables=(), publications=(PublicationInventory(product="stock-workbench:600519.SH", version="v1", stale=True),))
    def response(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "database": "ready"})
        return httpx.Response(200, json={"meta": {"source": "database", "version": "v1", "stale": True}, "data": {"isFallbackSample": False}})
    with httpx.Client(transport=httpx.MockTransport(response), base_url="http://testserver") as client:
        assert probe.check_api(client, expected)["workbenchCount"] == 1


def test_screener_checks_unclassified_inventory_and_exact_visible_symbols() -> None:
    probe = module()
    stocks = [{"symbol": "600001.SH", "board": "主板"}, {"symbol": "600002.SH", "board": ""}]
    assert probe._visible_symbols(stocks, 2) == {"600001.SH"}
    with pytest.raises(BackupError):
        probe._visible_symbols(stocks, 1)
    with pytest.raises(BackupError):
        probe._visible_symbols([stocks[1], stocks[1]], 2)

    expected = DatabaseInventory(tables=(), publications=(PublicationInventory(product="screener", version="v1", row_count=2),))
    def wrong_symbol(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "database": "ready"})
        return httpx.Response(200, json={"meta": {"source": "database", "version": "v1", "stale": False},
            "data": {"total": 1, "items": [{"symbol": "600002.SH"}]}})
    with httpx.Client(transport=httpx.MockTransport(wrong_symbol), base_url="http://testserver") as client:
        with pytest.raises(BackupError):
            probe.check_api(client, expected, screener_symbols={"600001.SH"})
