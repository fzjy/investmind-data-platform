from copy import deepcopy
from datetime import date
import importlib

import pytest
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError
from backend.tests.support.fixtures import FixtureStore
from backend.tests.support.providers import StocksFixtureProvider


META = DataMeta(source="database", asOf="2026-10-13", version="current", stale=True, staleReason="sample")
ROWS = [{"symbol": "600519.SH", "name": "贵州茅台"},
        {"symbol": "000001.SZ", "name": "平安银行", "isST": True}]


def models():
    return importlib.import_module("app.search.models")


class MemoryProvider:
    def __init__(self, raw, error=None):
        self.raw, self.error, self.read_count = raw, error, 0

    def read_screener_universe(self):
        self.read_count += 1
        if self.error is not None:
            raise self.error
        return ProviderSnapshot(deepcopy(self.raw), META)


def setup_service(rows=None, *, raw=None, error=None, settings=None):
    options = models().SearchOptions.from_settings(settings or Settings(_env_file=None))
    provider = MemoryProvider(raw if raw is not None else {"bootstrap": {}, "stocks": ROWS if rows is None else rows}, error)
    service = importlib.import_module("app.search.service").SearchService(provider, options)
    return service, provider


def query(service, q="", **kwargs):
    return service.query(models().SearchRequest(q=q, **kwargs), request_id="test-id")


def test_stock_outage_does_not_turn_into_empty_success():
    service, _ = setup_service(error=DataServiceError("DATA_NOT_READY", "private url/password"))
    result = query(service, "市场")
    assert result.data.stocks.status == "unavailable"
    assert result.data.stocks.total is None and result.data.stocks.meta is None
    assert result.data.stocks.error.code == "DATA_NOT_READY"
    assert "private" not in result.model_dump_json()
    assert [item.id for item in result.data.pages.items] == ["market"]


def test_empty_query_does_not_read_provider():
    service, provider = setup_service(error=RuntimeError("must not read"))
    result = query(service, " \t ")
    assert provider.read_count == 0
    assert result.data.query == ""
    assert result.data.stocks.status == "idle" and result.data.stocks.total is None
    assert result.data.stocks.meta is None and result.data.stocks.error is None
    assert {item.id for item in result.data.pages.items} == {"home", "market", "macro", "screener"}
    assert result.meta.request_id == "test-id" and result.meta.page_catalog_version == "site-pages-v1"


def test_empty_published_universe_is_ready():
    service, provider = setup_service([])
    result = query(service, "贵州")
    assert result.data.stocks.status == "ready" and result.data.stocks.total == 0
    assert result.data.stocks.meta == META and result.data.stocks.error is None
    assert provider.read_count == 1


def test_unexpected_provider_bug_is_not_hidden():
    service, _ = setup_service(error=RuntimeError("programming bug"))
    with pytest.raises(RuntimeError, match="programming bug"):
        query(service, "600519")


@pytest.mark.parametrize("code", ["INTERNAL_ERROR", "INVALID_REQUEST"])
def test_non_source_service_error_is_not_hidden(code):
    failure = DataServiceError(code, "non-source error")
    service, _ = setup_service(error=failure)
    with pytest.raises(DataServiceError) as caught:
        query(service, "市场")
    assert caught.value is failure


def test_database_error_is_safe_local_failure():
    service, _ = setup_service(error=OperationalError("private SQL", {}, Exception("password")))
    result = query(service, "宏观")
    assert result.data.stocks.error.code == "DATA_SOURCE_UNAVAILABLE"
    assert [item.id for item in result.data.pages.items] == ["macro"]
    assert "password" not in result.model_dump_json() and "private" not in result.model_dump_json()


@pytest.mark.parametrize("text,wanted", [("６００５１９.sh", "600519.SH"), ("000001", "000001.SZ"),
                                        ("贵州", "600519.SH"), ("银行", "000001.SZ")])
def test_code_name_normalization_and_st_inclusion(text, wanted):
    service, _ = setup_service()
    result = query(service, text)
    assert [item.symbol for item in result.data.stocks.items] == [wanted]
    item = result.data.stocks.items[0]
    assert item.code == wanted[:6] and item.href == "/stocks/" + wanted
    assert result.data.stocks.meta == META


def test_rank_is_exact_code_then_exact_name_prefix_and_substring():
    rows = [{"symbol": "600004.SH", "name": "X600519"}, {"symbol": "600003.SH", "name": "600519X"},
            {"symbol": "600002.SH", "name": "600519"}, {"symbol": "600519.SH", "name": "A"}]
    service, _ = setup_service(rows)
    assert [item.symbol for item in query(service, "600519").data.stocks.items] == [
        "600519.SH", "600002.SH", "600003.SH", "600004.SH"]


def test_equal_rank_sort_is_stable_and_only_public_fields_are_returned():
    service, _ = setup_service([{ "symbol": "600002.SH", "name": "测试", "secret": "not-returned"},
                               { "symbol": "600001.SH", "name": "测试"}])
    result = query(service, "测试")
    assert [item.symbol for item in result.data.stocks.items] == ["600001.SH", "600002.SH"]
    assert set(result.data.stocks.items[0].model_dump()) == {"symbol", "code", "name", "href"}
    assert "not-returned" not in result.model_dump_json()


@pytest.mark.parametrize("literal", ["%", "_"])
def test_wildcard_characters_are_literal(literal):
    service, _ = setup_service([{"symbol": "600001.SH", "name": "含" + literal},
                               {"symbol": "600002.SH", "name": "普通"}])
    assert [item.symbol for item in query(service, literal).data.stocks.items] == ["600001.SH"]


@pytest.mark.parametrize("raw", [{"bootstrap": {}, "stocks": "bad"}, {"bootstrap": {}, "stocks": [None]},
    {"stocks": ROWS}, {"bootstrap": {}, "stocks": [{"symbol": "bad", "name": "A"}]},
    {"bootstrap": {}, "stocks": [{"symbol": "600519.SH", "name": " "}]},
    {"bootstrap": {}, "stocks": [{"symbol": "600519.SH", "name": 123}]},
    {"bootstrap": {}, "stocks": [{"symbol": "600519.SH", "name": "A"}, {"symbol": "600519.sh", "name": "B"}]}])
def test_malformed_or_duplicate_source_is_not_silently_dropped(raw):
    service, _ = setup_service(raw=raw)
    result = query(service, "市场")
    assert result.data.stocks.status == "unavailable"
    assert result.data.stocks.error.code == "INVALID_SOURCE_PAYLOAD"
    assert [item.id for item in result.data.pages.items] == ["market"]


@pytest.mark.parametrize("text", ["a" * 65, "㍿" * 17])
def test_normalized_query_length_rejected_before_source_read(text):
    service, provider = setup_service()
    with pytest.raises(DataServiceError) as caught:
        query(service, text)
    assert caught.value.code == "INVALID_REQUEST" and provider.read_count == 0


def test_normalized_query_limit_inclusive_and_not_case_changed_for_display():
    service, _ = setup_service([])
    assert query(service, "㍿" * 16).data.query == "株式会社" * 16
    assert query(service, "a" * 64).data.query == "a" * 64
    assert query(service, "６００５１９.sh").data.query == "600519.sh"


def test_entire_snapshot_is_searched_before_pagination():
    service, provider = setup_service([{"symbol": f"{600000+i:06d}.SH", "name": f"测试{i}"} for i in range(250)])
    assert [item.symbol for item in query(service, "600249").data.stocks.items] == ["600249.SH"]
    assert provider.read_count == 1
    result = query(service, "测试", page=2, page_size=20)
    assert result.data.stocks.total == 250 and len(result.data.stocks.items) == 20
    assert result.data.stocks.items[0].symbol == "600020.SH"
    result = query(service, "测试", page=99)
    assert result.data.stocks.total == 250 and result.data.stocks.items == []


def test_real_fixture_provider_is_searched_without_synthetic_expansion():
    service = importlib.import_module("app.search.service").SearchService(
        StocksFixtureProvider(FixtureStore.default()), models().SearchOptions.from_settings(Settings(_env_file=None)))
    result = query(service, "600519")
    assert result.data.stocks.meta.source == "fixture"
    assert [item.symbol for item in result.data.stocks.items] == ["600519.SH"]
    assert query(service, "999999").data.stocks.total == 0


def test_page_catalog_is_not_stock_data_and_never_lists_placeholders():
    service, _ = setup_service()
    pages = query(service).data.pages.items
    assert {item.href for item in pages} == {"/", "/market", "/macro", "/stocks?mode=screener"}
    assert query(service, "筛选器").data.pages.items[0].id == "screener"
    assert query(service, "文章").data.pages.total == 0


@pytest.mark.parametrize("symbol", ["６００５１９.SH", "٦٠٠٥١٩.SH"])
@pytest.mark.parametrize("keyword", ["600519", "Example", "市场"])
def test_non_ascii_source_codes_fail_locally_instead_of_fake_zero(symbol, keyword):
    service, _ = setup_service([{"symbol": symbol, "name": "Example"}])
    result = query(service, keyword)
    assert result.data.stocks.status == "unavailable"
    assert result.data.stocks.total is None
    assert result.data.stocks.error.code == "INVALID_SOURCE_PAYLOAD"
    if keyword == "市场":
        assert result.data.pages.items[0].id == "market"
