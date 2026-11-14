import pytest
from pydantic import ValidationError

from app.config import Settings
from app.shared.errors import DataServiceError
from backend.tests.search.test_service import models, query, setup_service


def test_root_search_configuration_controls_real_service(monkeypatch):
    for key in ("QUERY_MAX_LENGTH", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE"):
        monkeypatch.delenv("DANG_SEARCH_" + key, raising=False)
    rows = [{"symbol": f"{600000+i:06d}.SH", "name": "测试"} for i in range(25)]
    service, _ = setup_service(rows)
    assert len(query(service, "测试").data.stocks.items) == 20
    with pytest.raises(DataServiceError):
        query(service, "测试", page_size=51)
    monkeypatch.setenv("DANG_SEARCH_QUERY_MAX_LENGTH", "10")
    monkeypatch.setenv("DANG_SEARCH_DEFAULT_PAGE_SIZE", "2")
    monkeypatch.setenv("DANG_SEARCH_MAX_PAGE_SIZE", "3")
    service, provider = setup_service(rows)
    assert len(query(service, "测试").data.stocks.items) == 2
    with pytest.raises(DataServiceError):
        query(service, "测试", page_size=4)
    with pytest.raises(DataServiceError):
        query(service, "a" * 11)


@pytest.mark.parametrize("values", [{"search_query_max_length": 0}, {"search_query_max_length": 129},
    {"search_default_page_size": 0}, {"search_max_page_size": 201},
    {"search_default_page_size": 3, "search_max_page_size": 2}])
def test_invalid_search_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        options = models().SearchOptions.from_settings(Settings(_env_file=None, **values))
