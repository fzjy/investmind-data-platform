from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.shared.errors import DataServiceError
from backend.tests.support.fixtures import FixtureStore
from backend.tests.support.providers import StocksFixtureProvider


def rpc(client, method, params=None):
    result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params or {}})
    assert result.status_code == 200, result.text[:300]
    return result.json()["result"]


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    # 此测试不需要真实数据库、采集凭据或后台采集。
    monkeypatch.setenv("DANG_DATABASE_READ_URL", "postgresql+psycopg://localhost/finance_test")
    from app import main
    path = tmp_path / "auth" / "finance.token"
    monkeypatch.setenv("INVESTMIND_FINANCE_TOKEN_FILE", str(path))
    provider = StocksFixtureProvider(FixtureStore.default())
    monkeypatch.setattr(main, "StocksDatabaseProvider", lambda *_: provider)
    def unavailable(_self, product, version):
        raise DataServiceError("DATA_NOT_FOUND", "指定的历史快照不存在")
    monkeypatch.setattr(main.SnapshotRepository, "read_versioned", unavailable)
    return main, path


def client_for(app):
    return TestClient(app, base_url="http://127.0.0.1:11384", client=("127.0.0.1", 30000))


def authorize(client, path):
    client.headers.update({"Authorization": "Bearer " + path.read_text().strip(),
                           "Accept": "application/json, text/event-stream",
                           "MCP-Protocol-Version": "2025-11-25"})


def test_optional_mount_http_auth_and_sidecar_scope(local_app):
    main, path = local_app
    with client_for(main.create_app(finance_mcp=False)) as legacy:
        assert legacy.get("/api/v1/search?q=600519").status_code == 200
        assert legacy.post("/mcp").status_code == 404
        assert not path.exists()
    with client_for(main.create_app(finance_only=True)) as client:
        assert client.get("/api/v1/search?q=600519").status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer incorrect"}).status_code == 401
        authorize(client, path)
        assert client.post("/mcp", headers={"Origin": "https://example.org"}).status_code == 403
        assert client.get("/api/v1/search", headers={"Host": "example.org"}).status_code == 403
        assert client.post("/api/v1/stocks/research", json={}).status_code == 403
        assert client.get("/api/v1/search?q=600519").status_code == 200


def test_lifespan_and_three_business_contracts(local_app, monkeypatch):
    from app.acquisition.contracts import (StateKey, StatusItem, StatusPage, Availability,
                                           Assessment, Evidence)
    from datetime import datetime, timezone
    main, path = local_app
    news = []
    @asynccontextmanager
    async def lifecycle(_app):
        news.append("start")
        yield
        news.append("stop")
    monkeypatch.setattr(main, "news_lifespan", lifecycle)
    app = main.create_app(background_news=True, finance_mcp=True)
    now = datetime.now(timezone.utc)
    def query(q):
        if q.limit == 0:
            from app.acquisition.contracts import QueryError
            raise QueryError("分页大小无效")
        return StatusPage("1", now, [
            StatusItem(StateKey("stock-dividend", "", "security", q.subject_keys[0]), "分红",
                       q.context, Evidence("unrecorded"), Availability(kind),
                       Assessment("excluded", "")) for kind in ("unknown", "missing", "confirmed_empty")], None)
    monkeypatch.setattr(app.state.acquisition_status, "query", query)
    with client_for(app) as client:
        assert news == ["start"]
        authorize(client, path)
        handshake = rpc(client, "initialize", {"protocolVersion": "2025-11-25",
                         "capabilities": {}, "clientInfo": {"name": "finance-test", "version": "1"}})
        assert handshake["protocolVersion"] == "2025-11-25"
        assert {t["name"] for t in rpc(client, "tools/list")["tools"]} == {
            "get_stock_workbench", "search_securities", "get_data_status", "get_evidence"}
        def call(name, **args):
            return rpc(client, "tools/call", {"name": name, "arguments": args})
        http = client.get("/api/v1/search?q=600519").json()
        search = call("search_securities", q="600519")["structuredContent"]
        assert search["data"] == http["data"] and set(search["meta"]) == {"requestId"}
        http = client.get("/api/v1/stocks/688525.SH").json()
        result = call("get_stock_workbench", symbol="688525.SH", version=http["meta"]["version"])
        assert result["structuredContent"] == {**http, "evidenceRefs": []}
        gap = call("get_stock_workbench", symbol="688525.SH", version="missing")
        assert gap["isError"] and gap["structuredContent"]["error"]["code"] == "DATA_NOT_FOUND"
        http = client.get("/api/v1/data-status?subjectKey=688525.SH").json()
        status = call("get_data_status", subjectKey="688525.SH")["structuredContent"]
        assert status == http
        assert [i["availability"]["kind"] for i in status["items"]] == ["unknown", "missing", "confirmed_empty"]
        unknown = call("search_securities", q="x", page=0)
        assert unknown["isError"] and unknown["structuredContent"]["error"]["code"] == "INVALID_REQUEST"
    assert news == ["start", "stop"]
