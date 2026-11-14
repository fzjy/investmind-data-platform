from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING, get_args
import uuid

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import URL

from app.config import Settings
from app.ops.backup.contracts import BackupError, ConnectionTarget, DatabaseInventory
from app.ops.backup.credentials import temporary_passfile
from app.ops.backup.process import run_process, runtime_environment
from app.shared.boards import normalize_board
from app.stocks.models import BoardId

if TYPE_CHECKING:
    import httpx


def configure_worker(target: ConnectionTarget) -> Settings:
    import app.config as config
    for name in tuple(os.environ):
        if name.upper().startswith(("DANG_", "COMPOSE_")):
            del os.environ[name]
    config.PROJECT_ENV_FILE = Path(tempfile.gettempdir()) / ("smartparse-missing-" + uuid.uuid4().hex + ".env")
    url = URL.create("postgresql+psycopg", username=target.username, password=target.password.get_secret_value(),
        host=target.host, port=target.port, database=target.database,
        query={"sslmode": target.sslmode, "connect_timeout": str(target.connect_timeout), "options": "-c default_transaction_read_only=on -c timezone=UTC"}).render_as_string(hide_password=False)
    settings = Settings(_env_file=None, data_backend="database", database_url=url, database_read_url=url)
    config.settings = settings
    return settings


def _mismatch() -> BackupError:
    return BackupError("restored_api_mismatch", 4, "verification")


def _visible_symbols(stocks: object, row_count: int | None) -> set[str]:
    """先验全集行数和证券身份，再按筛选器可选板块计算应返回的集合。"""
    if not isinstance(stocks, list) or len(stocks) != row_count:
        raise _mismatch()
    symbols: set[str] = set()
    visible: set[str] = set()
    for stock in stocks:
        if not isinstance(stock, dict):
            raise _mismatch()
        symbol = stock.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol in symbols:
            raise _mismatch()
        symbols.add(symbol)
        if "board" in stock and normalize_board(stock["board"], exchange=stock.get("exchange"), symbol=symbol) in get_args(BoardId):
            visible.add(symbol)
    return visible


def check_api(client: httpx.Client, expected: DatabaseInventory, *, screener_symbols: set[str] | None = None) -> dict[str, object]:
    health = client.get("/health")
    if health.status_code != 200 or health.json() != {"status": "ok", "database": "ready"}:
        raise _mismatch()
    summary = {"status": "ok", "apiCount": 1, "stockCount": 0, "workbenchCount": 0, "macroProducts": 0}
    request = {"filters": {"preset": None, "thresholds": {}, "enabledThresholds": [],
        "exclusions": [], "boards": list(get_args(BoardId)), "categories": {"majorIndustry": [], "subIndustry": [], "concept": [], "region": []}}, "sortRules": [], "page": 1, "pageSize": 200}
    for publication in expected.publications:
        product = publication.product
        if product != "screener" and not product.startswith("stock-workbench:") and product not in {"macro-transmission", "macro-insights"}:
            continue
        def read(page: int = 1):
            if product == "screener":
                response = client.post("/api/data/screener", json={**request, "page": page})
            else:
                path = "/api/data/stocks/" + product.split(":", 1)[1] if product.startswith("stock-workbench:") else "/api/data/" + product
                response = client.get(path)
            if response.status_code != 200:
                raise _mismatch()
            body = response.json()
            meta = body.get("meta", {})
            if meta.get("source") != "database" or meta.get("version") != publication.version or meta.get("stale") != publication.stale:
                raise _mismatch()
            summary["apiCount"] += 1
            return body.get("data", {})
        data = read()
        if product == "screener":
            count = len(screener_symbols) if screener_symbols is not None else publication.row_count
            if count is None or data.get("total") != count:
                raise _mismatch()
            symbols: set[str] = set()
            for page in range(1, max(1, (count + 199) // 200) + 1):
                page_data = data if page == 1 else read(page)
                items = page_data.get("items", [])
                if page_data.get("total") != count or len(items) != min(200, max(0, count - (page - 1) * 200)):
                    raise _mismatch()
                symbols.update(str(item.get("symbol")) for item in items)
            if len(symbols) != count or (screener_symbols is not None and symbols != screener_symbols):
                raise _mismatch()
            summary["stockCount"] = count
        elif product.startswith("stock-workbench:"):
            if data.get("isFallbackSample") is not False:
                raise _mismatch()
            summary["workbenchCount"] += 1
        else:
            summary["macroProducts"] += 1
    return summary


def probe_restored_api(target: ConnectionTarget, expected: DatabaseInventory, *, timeout: float) -> dict[str, object]:
    payload = {"target": target.model_dump(mode="json"), "expected": expected.model_dump(mode="json")}
    with temporary_passfile(target) as passfile:
        with tempfile.TemporaryFile(dir=passfile.parent) as incoming:
            incoming.write(json.dumps(payload, ensure_ascii=True).encode("ascii"))
            incoming.seek(0)
            try:
                output = run_process([sys.executable, "-I", str(Path(__file__).resolve())],
                    env={**runtime_environment(), "PGPASSFILE": str(passfile)}, timeout=timeout, stdin=incoming)
            except BackupError as error:
                if error.reason_code == "tool_failed" and error.process_returncode == 4:
                    raise _mismatch() from None
                raise
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise _mismatch()
        return result
    except (ValueError, TypeError):
        raise _mismatch() from None


def _passfile_fields(value: str) -> list[str]:
    fields = [""]
    escaped = False
    for char in value.rstrip("\n"):
        if escaped:
            fields[-1] += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("")
        else:
            fields[-1] += char
    if escaped or len(fields) != 5:
        raise _mismatch()
    return fields


def _worker() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024))
        fields = _passfile_fields(Path(os.environ["PGPASSFILE"]).read_text(encoding="utf-8"))
        target = ConnectionTarget(**request["target"], password=fields[4])
        if fields[:4] != [target.host, str(target.port), target.database, target.username]:
            raise _mismatch()
        expected = DatabaseInventory.model_validate(request["expected"])
        configure_worker(target)
        import httpx
        def deny_http(*args, **kwargs):
            raise _mismatch()
        async def deny_async_http(*args, **kwargs):
            raise _mismatch()
        httpx.HTTPTransport.handle_request = deny_http
        httpx.AsyncHTTPTransport.handle_async_request = deny_async_http
        try:
            import requests
        except ImportError:
            pass
        else:
            requests.Session.request = deny_http
            requests.Session.send = deny_http
        from fastapi.testclient import TestClient
        from sqlalchemy.orm import sessionmaker
        from app.main import create_app
        from app.ops.backup.service import open_source_engine
        from app.shared.repository import SnapshotRepository
        # 恢复验收是只读探针，不能启动生产后台任务。
        with TestClient(create_app(background_news=False)) as client:
            publication = next((p for p in expected.publications if p.product == "screener"), None)
            symbols = None
            if publication is not None:
                engine = open_source_engine(target)
                try:
                    stocks = SnapshotRepository(sessionmaker(engine)).read("screener").raw["stocks"]
                    symbols = _visible_symbols(stocks, publication.row_count)
                finally:
                    engine.dispose()
            result = check_api(client, expected, screener_symbols=symbols)
        print(json.dumps(result, ensure_ascii=True))
    except Exception:
        # 不打印任何 URL、HTTP正文、底层异常或供应商配置。
        print('{"status":"blocked","reasonCode":"restored_api_mismatch"}')
        raise SystemExit(4) from None


if __name__ == "__main__":
    _worker()
