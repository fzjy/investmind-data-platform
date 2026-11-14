from importlib import import_module

import pytest
from fastapi.testclient import TestClient

import backend.tests.support.application as main_module
from app.config import Settings
from app.shared.errors import DataServiceError
from backend.tests.search.test_service import MemoryProvider, ROWS


def controlled_client(monkeypatch, *, error=None, settings=None):
    provider = MemoryProvider({"bootstrap": {}, "stocks": ROWS}, error)
    def router(**kwargs):
        return import_module("app.search.routes").create_router(provider=provider, options=kwargs["options"])
    monkeypatch.setattr(main_module, "create_search_router", router, raising=False)
    monkeypatch.setattr(main_module, "settings", settings or Settings(_env_file=None))
    return TestClient(main_module.create_app(), raise_server_exceptions=False), provider


@pytest.mark.parametrize("suffix", ["?q=市场&q=宏观", "?q=市场&page=1&page=2",
    "?pageSize=1&pageSize=2", "?page=abc", "?page=0", "?pageSize=0", "?pageSize=51",
    "?q=" + "x" * 65, "?unexpected=value"])
def test_search_invalid_query_is_rejected_before_provider_read(monkeypatch, suffix):
    client, provider = controlled_client(monkeypatch)
    response = client.get("/api/data/search" + suffix)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert provider.read_count == 0


def test_search_duplicate_query_is_rejected_before_provider_read(monkeypatch):
    client, provider = controlled_client(monkeypatch)
    response = client.get("/api/data/search?q=市场&q=宏观")
    assert response.status_code == 400 and provider.read_count == 0


def test_application_registers_fixture_search(monkeypatch):
    monkeypatch.setattr(main_module, "settings", Settings(_env_file=None))
    response = TestClient(main_module.create_app()).get("/api/data/search?q=600519")
    assert response.status_code == 200
    body = response.json()
    assert [item["symbol"] for item in body["data"]["stocks"]["items"]] == ["600519.SH"]
    assert body["data"]["stocks"]["meta"]["source"] == "fixture"
    assert set(body["meta"]) == {"requestId"}


def test_v1_security_search_exposes_no_project_navigation(monkeypatch):
    client, provider = controlled_client(monkeypatch)
    body = client.get("/api/v1/search?q=600519").json()
    assert set(body["data"]) == {"query", "stocks"} and set(body["meta"]) == {"requestId"}
    assert body["data"]["stocks"]["items"] == [{"symbol":"600519.SH", "code":"600519", "name":"贵州茅台"}]
    assert provider.read_count == 1


def test_empty_input_and_wrong_method_do_not_read_provider(monkeypatch):
    client, provider = controlled_client(monkeypatch, error=RuntimeError("no read"))
    assert client.get("/api/data/search").json()["data"]["stocks"]["status"] == "idle"
    assert client.post("/api/data/search").status_code == 405
    assert provider.read_count == 0


def test_local_source_failure_preserves_security_unavailability(monkeypatch):
    client, _ = controlled_client(monkeypatch, error=DataServiceError("DATA_NOT_READY", "private password"))
    response = client.get("/api/data/search?q=宏观")
    assert response.status_code == 200
    body = response.json()["data"]
    assert set(body) == {"query","stocks"}
    assert body["stocks"]["status"] == "unavailable" and body["stocks"]["total"] is None
    assert body["stocks"]["error"]["code"] == "DATA_NOT_READY"
    assert "password" not in response.text


def test_unexpected_error_is_whole_request_failure(monkeypatch):
    client, _ = controlled_client(monkeypatch, error=RuntimeError("private traceback"))
    response = client.get("/api/data/search?q=市场")
    assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private" not in response.text


def test_configured_page_limit_is_used_by_real_application(monkeypatch):
    client, provider = controlled_client(monkeypatch, settings=Settings(_env_file=None,
        search_default_page_size=1, search_max_page_size=2))
    response = client.get("/api/data/search?q=6")
    assert response.status_code == 200 and response.json()["data"]["stocks"]["pageSize"] == 1
    assert client.get("/api/data/search?q=6&pageSize=3").status_code == 400
    assert provider.read_count == 1
