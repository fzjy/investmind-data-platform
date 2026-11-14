"""PBOC年表的合成合同测试；不是官方原始HTML或真实采集验收。"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult
from app.connectors.macro.pboc import PbocMacroProvider
from app.connectors.macro.transport import TextTransportResponse
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


class SyntheticTextTransport:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []

    def get_text(self, url: str) -> TextTransportResponse:
        self.calls.append(url)
        return TextTransportResponse(text=self.body, request_count=1)


def _annual_html(
    *,
    year: int = 2026,
    published_months: int = 12,
    header_style: str = "repeated",
) -> str:
    # 数值与HTML由测试构造，没有retrievedAt、原始响应hash或录制声明。
    first = '<td rowspan="3">项目 Items</td>' if header_style == "rowspan" else "<td>项目 Items</td>"
    period_first = "<td></td>" if header_style == "blank" else first
    chinese_first = "" if header_style == "rowspan" else "<td>项目 Items</td>"
    english_first = "" if header_style == "rowspan" else (
        "<td></td>" if header_style == "blank" else "<td>项目 Items</td>"
    )
    periods = "".join(f'<td colspan="2">{year}.{month}</td>' for month in range(1, 13))
    chinese = "<td>存量</td><td>增速（%）</td>" * 12
    english = "<td>Stock</td><td>Growth Rate(%)</td>" * 12
    data = "".join(
        f"<td>100.{month:02d}</td><td>1.{month:02d}</td>"
        if month <= published_months else "<td>&nbsp;</td><td></td>"
        for month in range(1, 13)
    )
    return (
        '<html><head><meta name="fixture-kind" content="synthetic"></head><body><table>'
        f"<tr>{period_first}{periods}</tr>"
        f"<tr>{chinese_first}{chinese}</tr>"
        f"<tr>{english_first}{english}</tr>"
        f"<tr><td>社会融资规模存量<br>AFRE(stock)</td>{data}</tr>"
        "</table></body></html>"
    )


def _fetch(
    body: str,
    *,
    source_url: str = "https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/09/2026091417325051118.htm",
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
) -> tuple[ProviderFetchResult, SyntheticTextTransport]:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.credit.tsf_stock_yoy")
    source = MacroSourceDefinition.model_validate({
        "provider": "pboc", "priority": 1, "series": "AFRE(stock)",
        "field": "Growth Rate(%)", "sourceUrl": source_url,
        "termsUrl": "https://www.pbc.gov.cn/rmyh/109345/index.html",
        "accessMode": "public_page", "displayAllowed": True,
        "responseFields": ["项目 Items", "存量", "增速（%）", "Stock", "Growth Rate(%)"],
    })
    transport = SyntheticTextTransport(body)
    result = PbocMacroProvider(transport).fetch(
        series, source, MacroFetchWindow(start=start, end=end, as_of=datetime(2027, 1, 1, tzinfo=UTC)),
    )
    return result, transport


def _assert_schema_failure(body: str) -> None:
    result, _ = _fetch(body)
    assert result.status == "failed"
    assert result.observations == ()
    assert result.error is not None and result.error.code == "PBOC_SCHEMA_INVALID"
    assert result.request_count == 1
    assert not result.error.retryable


@pytest.mark.parametrize("header_style", ["repeated", "blank", "rowspan"])
def test_synthetic_annual_headers_align_growth_columns(header_style: str) -> None:
    result, _ = _fetch(_annual_html(header_style=header_style))
    assert result.status == "success"
    assert len(result.observations) == 12
    assert result.observations[0].observed_at == date(2026, 1, 1)
    assert result.observations[0].value == Decimal("1.01")
    assert result.observations[-1].observed_at == date(2026, 12, 1)
    assert result.observations[-1].value == Decimal("1.12")
    assert all(item.released_at is None and item.revision is None for item in result.observations)


def test_synthetic_annual_trailing_empty_months_are_not_observations() -> None:
    result, _ = _fetch(_annual_html(published_months=8))
    assert result.status == "success"
    assert len(result.observations) == 8
    assert result.observations[-1].observed_at == date(2026, 8, 1)
    assert result.observations[-1].value == Decimal("1.08")
    assert result.raw_payload["periods"] == [
        "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01",
        "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01",
    ]


@pytest.mark.parametrize(
    ("start", "end", "expected_dates", "status"),
    [
        (date(2026, 8, 1), date(2026, 8, 31), [date(2026, 8, 1)], "success"),
        (date(2026, 9, 1), date(2026, 12, 31), [], "no_change"),
    ],
)
def test_synthetic_annual_window_does_not_invent_empty_month_values(
    start: date, end: date, expected_dates: list[date], status: str,
) -> None:
    result, _ = _fetch(_annual_html(published_months=8), start=start, end=end)
    assert result.status == status
    assert [item.observed_at for item in result.observations] == expected_dates
    assert result.raw_payload["periods"][-1] == "2026-08-01"


def test_synthetic_old_path_uses_header_year_not_attachment_year() -> None:
    old_url = "https://www.pbc.gov.cn/diaochatongjisi/fileDir/resource/cms/2018/04/2018041118085441632.htm"
    body = _annual_html(year=2016, header_style="blank").replace("AFRE(stock)", "AFRE（stock)")
    result, transport = _fetch(body, source_url=old_url, start=date(2016, 1, 1), end=date(2016, 12, 31))
    assert result.status == "success"
    assert len(result.observations) == 12
    assert result.observations[-1].observed_at == date(2016, 12, 1)
    assert result.observations[-1].value == Decimal("1.12")
    assert result.observations[-1].source_url == old_url
    assert transport.calls == [old_url]


def test_synthetic_cell_spacing_and_fullwidth_punctuation_preserve_exact_identity() -> None:
    body = _annual_html(header_style="blank").replace("项目 Items", "项目&nbsp;<br>Items")
    body = body.replace("AFRE(stock)", " AFRE（stock） ")
    body = body.replace("Growth Rate(%)", "Growth&nbsp;Rate（％）")
    result, _ = _fetch(body)
    assert result.status == "success"
    assert result.observations[0].value == Decimal("1.01")


def test_synthetic_blank_spacer_row_does_not_shift_total_row() -> None:
    body = _annual_html(header_style="blank").replace(
        "<tr><td>社会融资规模存量", '<tr><td colspan="25">&nbsp;</td></tr><tr><td>社会融资规模存量',
    )
    result, _ = _fetch(body)
    assert result.status == "success"
    assert result.observations[-1].value == Decimal("1.12")


@pytest.mark.parametrize("bad_value", ["NaN", "sNaN", "Infinity", "-Infinity", "invalid"])
@pytest.mark.parametrize("column", ["balance", "growth"])
def test_synthetic_annual_non_finite_or_invalid_numbers_fail_atomically(bad_value: str, column: str) -> None:
    old = "100.06" if column == "balance" else "1.06"
    _assert_schema_failure(_annual_html().replace(f"<td>{old}</td>", f"<td>{bad_value}</td>"))


@pytest.mark.parametrize(
    "body",
    [
        _annual_html().replace("<td>100.06</td>", "<td></td>"),
        _annual_html().replace("<td>1.06</td>", "<td></td>"),
        _annual_html().replace("<td>100.06</td><td>1.06</td>", "<td></td><td></td>"),
        _annual_html(published_months=0),
        _annual_html().replace("2026.6</td>", "2026.5</td>"),
        _annual_html().replace("2026.6</td>", "2025.6</td>"),
        _annual_html().replace("2026.6</td>", "2026.13</td>"),
        _annual_html().replace("2026.6</td>", "0000.6</td>"),
        _annual_html().replace('<td colspan="2">2026.6</td>', "<td>2026.6</td><td>2026.7</td>"),
        _annual_html().replace("AFRE(stock)", "AFRE(flow)"),
        _annual_html().replace("社会融资规模存量", "北京市社会融资规模存量"),
        _annual_html().replace("Growth Rate(%)", "Growth(%)"),
    ],
    ids=["missing-balance", "missing-growth", "interior-gap", "all-blank", "duplicate-period",
         "mixed-years", "invalid-month", "invalid-year", "mismatched-pair", "flow", "regional", "header-drift"],
)
def test_synthetic_annual_ambiguous_or_incomplete_contract_fails(body: str) -> None:
    _assert_schema_failure(body)


def test_synthetic_duplicate_total_rows_fail_instead_of_first_row_wins() -> None:
    body = _annual_html()
    start = body.index("<tr><td>社会融资规模存量")
    end = body.index("</tr>", start) + len("</tr>")
    _assert_schema_failure(body.replace("</table>", body[start:end] + "</table>"))


def test_synthetic_duplicate_matching_tables_fail_instead_of_first_table_wins() -> None:
    _assert_schema_failure(_annual_html() + _annual_html())


def test_synthetic_numeric_colspan_cannot_copy_balance_into_growth() -> None:
    _assert_schema_failure(_annual_html().replace(
        "<td>100.06</td><td>1.06</td>", '<td colspan="2">100.06</td>',
    ))


def test_synthetic_numeric_rowspan_cannot_supply_growth_from_a_component_row() -> None:
    body = _annual_html()
    total = body.index("<tr><td>社会融资规模存量")
    component = (
        "<tr><td>其中:人民币贷款</td>"
        + "<td>10.00</td><td>2.00</td>" * 5
        + '<td>10.00</td><td rowspan="2">88.8</td>'
        + "<td>10.00</td><td>2.00</td>" * 6
        + "</tr>"
    )
    body = body[:total] + component + body[total:]
    _assert_schema_failure(body.replace("<td>100.06</td><td>1.06</td>", "<td>100.06</td>"))


def test_synthetic_total_row_before_headers_is_still_duplicate_identity() -> None:
    body = _annual_html()
    start = body.index("<tr><td>社会融资规模存量")
    total = body[start:body.index("</tr>", start) + len("</tr>")]
    _assert_schema_failure(body.replace("<table>", "<table>" + total, 1))


def test_synthetic_second_total_table_with_broken_chinese_header_is_not_ignored() -> None:
    _assert_schema_failure(_annual_html() + _annual_html().replace("增速（%）", "增速"))


@pytest.mark.parametrize("suffix", ["<table", '<table class="'])
def test_synthetic_truncated_start_tag_cannot_be_success(suffix: str) -> None:
    _assert_schema_failure(_annual_html() + suffix)


def test_synthetic_merged_empty_tail_pair_is_not_a_numeric_observation() -> None:
    body = _annual_html(published_months=8).replace(
        "<td>&nbsp;</td><td></td>", '<td colspan="2">&nbsp;</td>',
    )
    result, _ = _fetch(body)
    assert result.status == "success"
    assert len(result.observations) == 8
    assert result.observations[-1].value == Decimal("1.08")


def test_synthetic_zero_is_a_real_growth_value_not_an_empty_cell() -> None:
    result, _ = _fetch(_annual_html().replace("<td>1.06</td>", "<td>0</td>"))
    assert result.status == "success"
    assert result.observations[5].observed_at == date(2026, 6, 1)
    assert result.observations[5].value == Decimal("0")


@pytest.mark.parametrize("span", ["0", "-1", "invalid", "1000000000"])
def test_synthetic_invalid_rowspan_fails_closed(span: str) -> None:
    _assert_schema_failure(_annual_html(header_style="rowspan").replace('rowspan="3"', f'rowspan="{span}"'))


def test_synthetic_unclosed_table_cannot_be_partial_success() -> None:
    _assert_schema_failure(_annual_html() + "<table><tr><td>")


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.pbc.gov.cn/diaochatongjisi/fileDir/resource/cms/2018/04/2018041118085441632.html",
        "https://www.pbc.gov.cn/diaochatongjisi/fileDir/resource/cms/2018/04/2018041118085441632.htm?download=1",
        "https://www.pbc.gov.cn/diaochatongjisi/fileDir/resource/cms/2018/04/2018041118085441632.htm#table",
        "https://www.pbc.gov.cn/fileDir/resource/cms/2018/04/2018041118085441632.htm",
    ],
    ids=["old-suffix", "old-query", "old-fragment", "old-wrong-prefix"],
)
def test_synthetic_old_path_does_not_relax_url_boundaries(source_url: str) -> None:
    result, transport = _fetch(_annual_html(), source_url=source_url)
    assert result.status == "failed"
    assert result.error is not None and result.error.code == "PBOC_PATH_NOT_ALLOWED"
    assert result.request_count == 0
    assert transport.calls == []
