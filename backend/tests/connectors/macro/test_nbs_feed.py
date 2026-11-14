from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderTransientError
from app.connectors.macro.nbs import NbsMacroProvider
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


INDEX = "https://www.stats.gov.cn/sj/zxfb/"
CPI_URL = INDEX + "202609/t20260909_1965263.html"
INDUSTRY_URL = INDEX + "202609/t20260915_1965308.html"
CPI_TITLE = "2026年8月份居民消费价格同比上涨0.8%"
INDUSTRY_TITLE = "2026年8月份规模以上工业增加值增长5.2%"
CPI = f'''<meta name="ArticleTitle" content="{CPI_TITLE}">
<meta name="PubDate" content="2026/09/09 09:30">
<p>2026年8月份，全国居民消费价格同比上涨0.8%。</p>
<table><tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>1—8月同比涨跌幅（%）</td></tr>
<tr><td>其中：不包括食品和能源</td><td>0.1</td><td>1.0</td><td>1.1</td></tr></table>'''
INDUSTRY = f'''<meta name="ArticleTitle" content="{INDUSTRY_TITLE}">
<meta name="PubDate" content="2026/09/15 10:00">
<p>8月份，规模以上工业增加值同比实际增长5.2%（增加值增速均为扣除价格因素的实际增长率）。
1—8月份，规模以上工业增加值同比增长5.3%。</p>'''

# 根据2026-05-18官方报告手工核对的最小结构；累计5.6不是单月4.1。
CUMULATIVE_URL = INDEX + "202605/t20260518_1963731.html"
CUMULATIVE_TITLE = "2026年1—4月份规模以上工业增加值增长5.6%"
CUMULATIVE_TABLE = '''<p>2026年4月份规模以上工业生产主要数据</p>
<table><tr><td rowspan="2">指标</td><td colspan="2">4月</td><td colspan="2">1—4月</td></tr>
<tr><td>绝对量</td><td>同比增长 （%）</td><td>绝对量</td><td>同比增长 （%）</td></tr>
<tr><td>规模以上工业增加值</td><td>…</td><td>4.1</td><td>…</td><td>5.6</td></tr></table>'''
CUMULATIVE = f'''<meta name="ArticleTitle" content="{CUMULATIVE_TITLE}">
<meta name="PubDate" content="2026/05/18 10:00">
<p>1—4月份，规模以上工业增加值同比实际增长5.6%（增加值增速均为扣除价格因素的实际增长率）。
4月份，规模以上工业增加值同比增长4.1%。从环比看，4月份，规模以上工业增加值比上月增长0.05%。</p>
{CUMULATIVE_TABLE}'''


class Transport:
    def __init__(self, pages: dict[str, str | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get_text(self, url: str) -> TextTransportResponse:
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return TextTransportResponse(value, request_count=1)


def source(series_id: str, pages: str = "2") -> MacroSourceDefinition:
    return MacroSourceDefinition.model_validate({
        "provider": "nbs", "priority": 1,
        "series": "不包括食品和能源" if series_id == "cn.cpi.core_yoy" else "规模以上工业增加值",
        "field": "同比涨跌幅（%）" if series_id == "cn.cpi.core_yoy" else "同比实际增长（%）",
        "sourceUrl": INDEX, "termsUrl": "https://www.stats.gov.cn/",
        "accessMode": "public_page", "displayAllowed": True,
        "params": {"maxIndexPages": pages}, "responseFields": ["ArticleTitle", "PubDate"],
    })


def window(start: date = date(2026, 8, 1)) -> MacroFetchWindow:
    return MacroFetchWindow(start, date(2026, 9, 30), datetime(2026, 9, 30, 13, tzinfo=UTC))


def index(title: str, url: str, count: int = 1, page: int = 0) -> str:
    return f'''<a href="{url}" title="{title}">{title}</a>
    <a href="{url}">{title}</a><script>createPageHTML({count}, {page}, "index", "html");</script>'''


@pytest.mark.parametrize(("series_id", "title", "url", "body", "value", "hour"), [
    ("cn.cpi.core_yoy", CPI_TITLE, CPI_URL, CPI, "1.0", 1),
    ("cn.industry.value_added_yoy", INDUSTRY_TITLE, INDUSTRY_URL, INDUSTRY, "5.2", 2),
])
def test_discovers_and_validates_monthly_official_release(series_id, title, url, body, value, hour):
    transport = Transport({INDEX: index(title, url), url: body})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(series_id)
    result = NbsMacroProvider(transport).fetch(series, source(series_id), window())
    assert result.status == "success"
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.observed_at == date(2026, 8, 1)
    assert observation.value == Decimal(value)
    assert observation.source_url == url
    assert observation.released_at.hour == hour
    assert result.request_count == 2
    assert transport.calls == [INDEX, url]
    assert "failureContext" not in result.raw_payload


def test_feed_follows_verified_pagination_and_reports_budget_gap():
    older_url = INDEX + "202608/t20260812_1964000.html"
    older_title = CPI_TITLE.replace("8月份", "7月份")
    transport = Transport({
        INDEX: index(CPI_TITLE, CPI_URL, 3), CPI_URL: CPI,
        INDEX + "index_1.html": index(older_title, older_url, 3, 1),
        older_url: CPI.replace("8月份", "7月份").replace("2026/09/09", "2026/08/12"),
    })
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 1, 1)))
    assert result.status == "success"
    assert [o.observed_at for o in result.observations] == [date(2026, 7, 1), date(2026, 8, 1)]
    assert result.raw_payload["missingWindows"] == [{"start": "2026-01-01", "end": "2026-06-30"}]
    assert result.request_count == 4


@pytest.mark.parametrize("body", [CPI.replace("同比涨跌幅（%）", "同比值（%）"), CPI.replace("<td>1.0</td>", "<td>--</td>"), CPI.replace("2026/09/09 09:30", "not-a-date")])
def test_feed_does_not_invent_values_or_dates_on_schema_drift(body):
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: body})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None


def test_industry_rejects_mismatch_and_does_not_fetch_cumulative_release():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    transport = Transport({INDEX: index(INDUSTRY_TITLE, INDUSTRY_URL), INDUSTRY_URL: INDUSTRY.replace("同比实际增长5.2%", "同比实际增长5.3%")})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "failed"
    assert result.error.code == "NBS_VALUE_MISMATCH"
    transport = Transport({INDEX: index("2026年1—2月份规模以上工业增加值增长5.2%", INDUSTRY_URL)})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.observations == ()
    assert transport.calls == [INDEX]


def test_feed_preserves_request_count_on_transport_failure():
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: ProviderTransientError(request_count=3)})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "failed"
    assert result.request_count == 4


def test_industry_does_not_report_unpublished_january_february_prefix():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    pages = {}
    listing = []
    for month in range(3, 9):
        url = INDEX + f"202609/t20260915_19653{month:02d}.html"
        title = INDUSTRY_TITLE.replace("8月份", f"{month}月份")
        listing.append(index(title, url))
        pages[url] = INDUSTRY.replace("8月份", f"{month}月份")
    pages[INDEX] = "".join(listing)
    result = NbsMacroProvider(Transport(pages)).fetch(series, source(series.id), window(date(2026, 1, 1)))
    assert len(result.observations) == 6
    assert result.raw_payload["missingWindows"] == []


@pytest.mark.parametrize(("second_value", "status"), [("1.0", "success"), ("9.0", "failed")])
def test_core_cpi_deduplicates_matching_desktop_mobile_tables_only(second_value, status):
    duplicate = CPI[CPI.index("<table>"):].replace("<td>1.0</td>", f"<td>{second_value}</td>")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: CPI + duplicate})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == status


@pytest.mark.parametrize("page_limit", ["0", "-1", "wrong", "1001"])
def test_feed_rejects_invalid_budget_before_network(page_limit):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    transport = Transport({})
    result = NbsMacroProvider(transport).fetch(series, source(series.id, page_limit), window())
    assert result.status == "failed"
    assert transport.calls == []


@pytest.mark.parametrize("url", ["https://example.com/release.html", INDEX + "202609/t20260909_1965263.html?key=x", "https://reader:secret@www.stats.gov.cn/sj/zxfb/202609/t20260909_1965263.html"])
def test_matching_release_link_cannot_escape_official_path_contract(url):
    transport = Transport({INDEX: index(CPI_TITLE, url)})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "failed"
    assert transport.calls == [INDEX]


def test_invalid_year_and_early_monthly_release_have_accounted_schema_failures():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    for title, body in [(CPI_TITLE.replace("2026", "0000"), CPI.replace("2026", "0000")),
                        (CPI_TITLE, CPI.replace("2026/09/09", "2026/08/15"))]:
        transport = Transport({INDEX: index(title, CPI_URL), CPI_URL: body})
        result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
        assert result.status == "failed"
        assert result.request_count == len(transport.calls)


def test_future_release_is_not_visible_before_its_published_time():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: CPI})
    early = MacroFetchWindow(date(2026, 8, 1), date(2026, 9, 9), datetime(2026, 9, 9, 1, 29, tzinfo=UTC))
    result = NbsMacroProvider(transport).fetch(series, source(series.id), early)
    assert result.status == "no_change" and result.observations == ()


def test_release_on_next_month_midnight_uses_china_calendar_not_utc_date():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: CPI.replace("2026/09/09 09:30", "2026/09/01 00:30")})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "success"
    assert result.observations[0].released_at == datetime(2026, 8, 31, 16, 30, tzinfo=UTC)


@pytest.mark.parametrize("future_june", [False, True])
def test_internal_missing_months_and_filtered_future_rows_remain_history_gaps(future_june):
    june_url = INDEX + "202607/t20260709_1964001.html"
    june_title = CPI_TITLE.replace("8月份", "6月份")
    june = CPI.replace("8月份", "6月份").replace("2026/09/09", "2026/10/09" if future_june else "2026/07/09")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL) + index(june_title, june_url), CPI_URL: CPI, june_url: june})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 6, 1)))
    assert result.status == "success"
    assert result.raw_payload["missingWindows"] == [{"start": "2026-06-01" if future_june else "2026-07-01", "end": "2026-07-31"}]


def test_out_of_window_unapproved_link_cannot_stop_archive_or_prove_coverage():
    bad_url = "https://example.com/202607/t20260709_1964001.html"
    older_title = CPI_TITLE.replace("8月份", "6月份")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL, 2) + index(older_title, bad_url, 2), CPI_URL: CPI})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 7, 1)))
    assert result.status == "failed"
    assert result.error.code == "NBS_PATH_NOT_ALLOWED"
    assert bad_url not in transport.calls
    context = result.raw_payload["failureContext"]
    assert context["stage"] == "index" and context["url"] == INDEX
    assert context["textSha256"] == sha256(transport.pages[INDEX].encode()).hexdigest()
    assert bad_url not in json.dumps(result.raw_payload)


@pytest.mark.parametrize("body,actual", [
    ("", None),
    ('<html>api_key=not-for-audit</html>', None),
    ('<script>createPageHTML(3, 9, "index", "html");</script>',
     {"pageCount": "3", "pageIndex": "9"}),
])
def test_failed_index_keeps_response_evidence_without_body_or_retry(body, actual):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(Transport({INDEX: body})).fetch(series, source(series.id), window())
    assert result.status == "failed" and result.error.code == "NBS_INDEX_SCHEMA_INVALID"
    assert result.observations == () and result.request_count == 1
    assert result.error.retryable is False
    context = result.raw_payload.get("failureContext")
    assert context is not None, "失败目录响应必须保留诊断摘要"
    assert context == {
        "stage": "index", "url": INDEX, "pageIndex": 0, "responseReceived": True,
        "textSha256": sha256(body.encode()).hexdigest(), "textBytes": len(body.encode()),
        "emptyBody": not body.strip(), "hasReplacementCharacters": False,
        "pagination": actual,
    }
    assert "not-for-audit" not in json.dumps(result.raw_payload)


def test_later_index_failure_identifies_failed_page_not_last_valid_release():
    failed_url = INDEX + "index_1.html"
    body = '<script>createPageHTML(2, 0, "index", "html");</script>'
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL, 2), CPI_URL: CPI, failed_url: body})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 1, 1)))
    context = result.raw_payload.get("failureContext")
    assert context is not None
    assert context["url"] == failed_url and context["pageIndex"] == 1
    assert context["pagination"] == {"pageCount": "2", "pageIndex": "0"}
    assert result.status == "failed" and result.observations == ()
    assert result.request_count == 3 and len(result.raw_payload["releases"]) == 1


@pytest.mark.parametrize("actual_title", [None, CPI_TITLE.replace("8月份", "7月份")])
def test_release_title_failure_keeps_expected_and_actual_identity(actual_title):
    body = '<html>body-marker-must-not-be-retained'
    if actual_title is not None:
        body += f'<meta name="ArticleTitle" content="{actual_title}">'
    body += '<meta name="PubDate" content="2026/09/09 09:30"></html>'
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: body})).fetch(
        series, source(series.id), window())
    context = result.raw_payload.get("failureContext")
    assert context is not None
    assert context["stage"] == "release" and context["url"] == CPI_URL
    assert context["expectedTitle"] == CPI_TITLE
    assert context["articleTitle"] == actual_title
    assert context["pubDate"] == "2026/09/09 09:30"
    assert context["textSha256"] == sha256(body.encode()).hexdigest()
    assert result.error.code == "NBS_TITLE_MISMATCH" and not result.error.retryable
    assert result.observations == () and result.request_count == 2
    assert "body-marker-must-not-be-retained" not in json.dumps(result.raw_payload)


def test_failure_metadata_is_bounded_and_sanitized_without_masking_original_error():
    title = 'api_key=do-not-store ' + 'x' * 3000
    body = (f'<meta name="ArticleTitle" content="{title}">'
            '<meta name="PubDate" content="password=do-not-store">\ufffd')
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: body})).fetch(
        series, source(series.id), window())
    context = result.raw_payload.get("failureContext")
    assert context is not None
    assert len(context["articleTitle"]) <= 512
    assert context["hasReplacementCharacters"] is True
    assert "do-not-store" not in json.dumps(result.raw_payload)
    assert result.error.code == "NBS_TITLE_MISMATCH"


def test_failed_transport_does_not_reuse_previous_response_evidence():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    transport = Transport({INDEX: index(CPI_TITLE, CPI_URL), CPI_URL: ProviderTransientError(request_count=3)})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.raw_payload.get("failureContext") == {
        "stage": "release", "url": CPI_URL, "pageIndex": 0,
        "responseReceived": False, "expectedTitle": CPI_TITLE,
    }
    assert result.request_count == 4 and result.error.retryable is True


def test_validation_failure_has_no_fictional_response_context():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(Transport({})).fetch(series, source(series.id, "0"), window())
    assert result.status == "failed" and result.request_count == 0
    assert "failureContext" not in result.raw_payload


def test_failure_context_bounds_pagination_without_changing_validation():
    digits = "9" * 1024
    body = f'<script>createPageHTML({digits}, 0, "index", "html");</script>'
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(Transport({INDEX: body})).fetch(series, source(series.id), window())
    context = result.raw_payload["failureContext"]
    assert len(context["pagination"]["pageCount"]) <= 512
    assert context["truncatedFields"]["pagination.pageCount"] == {
        "characters": 1024, "sha256": sha256(digits.encode()).hexdigest(),
    }
    assert result.error.code == "NBS_INDEX_SCHEMA_INVALID"
    assert result.request_count == 1 and result.observations == ()


def test_failure_context_bounds_official_url_without_changing_request_target():
    long_url = INDEX + "202609/t20260909_" + "9" * 10000 + ".html"
    transport = Transport({INDEX: index(CPI_TITLE, long_url),
                           long_url: ProviderTransientError(request_count=1)})
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    context = result.raw_payload["failureContext"]
    assert len(context["url"]) <= 512
    assert context["truncatedFields"]["url"] == {
        "characters": len(long_url), "sha256": sha256(long_url.encode()).hexdigest(),
    }
    assert transport.calls == [INDEX, long_url]
    assert result.request_count == 2 and result.observations == ()
    assert result.error.code == "PROVIDER_TRANSIENT_FAILURE"


@pytest.mark.parametrize("body,value", [
    (CUMULATIVE, "4.1"),
    (CUMULATIVE + CUMULATIVE_TABLE, "4.1"),
    (CUMULATIVE.replace("同比增长4.1%", "同比下降4.1%").replace("<td>4.1</td>", "<td>-4.1</td>"), "-4.1"),
], ids=["official-layout", "matching-desktop-mobile", "monthly-decline"])
def test_cumulative_industry_release_extracts_verified_monthly_not_cumulative_or_mom(body, value):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    transport = Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL), CUMULATIVE_URL: body})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "success"
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.observed_at == date(2026, 4, 1)
    assert observation.value == Decimal(value)
    assert observation.released_at == datetime(2026, 5, 18, 2, tzinfo=UTC)
    assert observation.source_url == CUMULATIVE_URL
    assert result.raw_payload["missingWindows"] == []
    assert result.raw_payload["releases"][0]["articleTitle"] == CUMULATIVE_TITLE
    assert result.raw_payload["releases"][0]["value"] == value
    assert result.request_count == 2 and transport.calls == [INDEX, CUMULATIVE_URL]
    assert "failureContext" not in result.raw_payload


@pytest.mark.parametrize("body,code", [
    (CUMULATIVE.replace("4月份，规模以上工业增加值同比增长4.1%", "3月份，规模以上工业增加值同比增长4.1%"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("4月份，规模以上工业增加值同比增长4.1%。", ""), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace("同比实际增长5.6%", "同比实际增长5.7%"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("1—4月份，规模以上工业增加值同比实际增长5.6%", "1—3月份，规模以上工业增加值同比实际增长5.6%"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("<td>4.1</td>", "<td>5.6</td>"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("<td>5.6</td>", "<td>4.1</td>"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("<td>4.1</td>", "<td>--</td>"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace("<td>4.1</td>", "<td>NaN</td>"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace("<td>5.6</td>", "<td>Infinity</td>"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace('colspan="2">4月', 'colspan="2">3月'), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace("同比增长 （%）", "环比增长（%）"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace("2026年4月份规模以上工业生产主要数据", "2025年4月份规模以上工业生产主要数据"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE.replace(CUMULATIVE_TABLE, ""), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE + CUMULATIVE_TABLE.replace("<td>4.1</td>", "<td>4.2</td>"), "NBS_VALUE_MISMATCH"),
    (CUMULATIVE + "<p>4月份，规模以上工业增加值同比增长4.2%。</p>", "NBS_VALUE_MISMATCH"),
    (CUMULATIVE.replace("2026/05/18 10:00", "2026/04/18 10:00"), "NBS_RELEASE_DATE_INVALID"),
    (CUMULATIVE.replace(f'content="{CUMULATIVE_TITLE}"', 'content="2026年1—5月份规模以上工业增加值增长5.6%"'), "NBS_TITLE_MISMATCH"),
], ids=["wrong-body-month", "missing-monthly-body", "wrong-cumulative-body-value",
        "wrong-cumulative-body-month", "wrong-monthly-table-value", "wrong-cumulative-table-value",
        "missing-value", "nan", "infinity", "wrong-table-month", "mom-header", "wrong-table-year",
        "no-table", "conflicting-mobile-table", "conflicting-body", "early-publication", "title-mismatch"])
def test_cumulative_industry_release_rejects_missing_conflicting_or_wrong_period_evidence(body, code):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL),
                                      CUMULATIVE_URL: body})).fetch(
        series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "failed"
    assert result.observations == () and result.request_count == 2
    assert result.error is not None and result.error.retryable is False
    assert result.error.code == code
    context = result.raw_payload["failureContext"]
    assert context["stage"] == "release" and context["url"] == CUMULATIVE_URL
    assert context["expectedTitle"] == CUMULATIVE_TITLE


def test_cumulative_industry_release_preserves_window_and_publication_visibility():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    transport = Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL), CUMULATIVE_URL: CUMULATIVE})
    early = MacroFetchWindow(date(2026, 4, 1), date(2026, 4, 30), datetime(2026, 5, 18, 1, 59, tzinfo=UTC))
    result = NbsMacroProvider(transport).fetch(series, source(series.id), early)
    assert result.status == "no_change" and result.observations == ()
    assert result.raw_payload["missingWindows"] == [{"start": "2026-04-01", "end": "2026-04-30"}]
    assert result.request_count == 2
    transport = Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL)})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window())
    assert result.status == "no_change" and result.observations == ()
    assert transport.calls == [INDEX]


@pytest.mark.parametrize("month,observed,published", [
    (3, date(2026, 3, 1), "2026/04/18 10:00"),
    (12, date(2026, 12, 1), "2027/01/18 10:00"),
])
def test_cumulative_industry_contract_uses_end_month_across_year_boundary(month, observed, published):
    title = CUMULATIVE_TITLE.replace("4月份", f"{month}月份")
    body = CUMULATIVE.replace("4月份", f"{month}月份").replace("4月", f"{month}月").replace("2026/05/18 10:00", published)
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(title, CUMULATIVE_URL), CUMULATIVE_URL: body})).fetch(
        series, source(series.id), MacroFetchWindow(observed, observed, datetime(2027, 2, 1, tzinfo=UTC)))
    assert result.status == "success"
    assert result.observations[0].observed_at == observed
    assert result.observations[0].value == Decimal("4.1")


def test_cumulative_industry_handles_inline_spans_and_repeated_body_without_inventing_conflicts():
    body = CUMULATIVE.replace("规模以上工业增加值同比", "规模以上<span>工业增加值</span>同比")
    body += "<p>4月份，规模以上工业增加值同比增长4.1%。</p>"
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL), CUMULATIVE_URL: body})).fetch(
        series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "success" and result.observations[0].value == Decimal("4.1")


def test_cumulative_industry_does_not_clear_other_missing_months_or_exceed_budget():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    transport = Transport({INDEX: index(INDUSTRY_TITLE, INDUSTRY_URL, 3), INDUSTRY_URL: INDUSTRY,
                           INDEX + "index_1.html": index(CUMULATIVE_TITLE, CUMULATIVE_URL, 3, 1),
                           CUMULATIVE_URL: CUMULATIVE})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "success"
    assert [o.observed_at for o in result.observations] == [date(2026, 4, 1), date(2026, 8, 1)]
    assert result.raw_payload["missingWindows"] == [{"start": "2026-05-01", "end": "2026-07-31"}]
    assert result.request_count == 4
    assert INDEX + "index_2.html" not in transport.calls


@pytest.mark.parametrize("title", [
    "2026年1—1月份规模以上工业增加值增长5.6%",
    "2026年1—2月份规模以上工业增加值增长5.6%",
    "2026年1—13月份规模以上工业增加值增长5.6%",
    "2026年2—4月份规模以上工业增加值增长5.6%",
    "2026年1-4月份规模以上工业增加值增长5.6%",
])
def test_unverified_cumulative_title_forms_are_not_fetched_or_split(title):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    transport = Transport({INDEX: index(title, CUMULATIVE_URL)})
    result = NbsMacroProvider(transport).fetch(series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "no_change" and result.observations == ()
    assert result.raw_payload["missingWindows"] == [{"start": "2026-04-01", "end": "2026-09-30"}]
    assert transport.calls == [INDEX]


def test_cumulative_body_cannot_match_suffix_of_an_unverified_range():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    body = CUMULATIVE.replace("1—4月份，", "21—4月份，")
    result = NbsMacroProvider(Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL), CUMULATIVE_URL: body})).fetch(
        series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "failed" and result.observations == ()
    assert result.error.code == "NBS_SCHEMA_INVALID"


@pytest.mark.parametrize("extra,code", [
    ("<p>1—4月份，规模以上工业增加值同比增长5.7%。</p>", "NBS_VALUE_MISMATCH"),
    ("<p>4月份，规模以上工业增加值同比增长NaN%。</p>", "NBS_SCHEMA_INVALID"),
    ("<p>4月份，规模以上工业增加值同比增长--%。</p>", "NBS_SCHEMA_INVALID"),
    ("<p>1—4月份，规模以上工业增加值同比实际增长Infinity%。</p>", "NBS_SCHEMA_INVALID"),
    ("<p>4月份，规模以上工业增加值同比增长--。</p>", "NBS_SCHEMA_INVALID"),
    (CUMULATIVE_TABLE.replace("2026年4月份", "2025年4月份"), "NBS_SCHEMA_INVALID"),
    (CUMULATIVE_TABLE.replace("<p>2026年4月份规模以上工业生产主要数据</p>", ""), "NBS_SCHEMA_INVALID"),
], ids=["cumulative-conflict-without-actual", "monthly-nan", "monthly-missing", "cumulative-infinite",
        "missing-percent", "mobile-wrong-year", "mobile-no-caption"])
def test_cumulative_industry_valid_evidence_cannot_hide_invalid_repeated_evidence(extra, code):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.industry.value_added_yoy")
    result = NbsMacroProvider(Transport({INDEX: index(CUMULATIVE_TITLE, CUMULATIVE_URL),
                                      CUMULATIVE_URL: CUMULATIVE + extra})).fetch(
        series, source(series.id), window(date(2026, 4, 1)))
    assert result.status == "failed" and result.observations == ()
    assert result.error.code == code
    assert result.request_count == 2
