"""财政官方 HTML 纯解析边界；受控夹具不冒充真实原件验收。"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util

import pytest


TITLE = "社会融资规模增量统计表"
UNIT = "单位：亿元人民币"


def parse(body: bytes, year: int = 2026) -> dict:
    # 模块尚不存在时产生明确的功能缺失断言，而不是收集错误。
    assert importlib.util.find_spec("app.macro.fiscal_official") is not None, "财政纯解析器尚未实现"
    return importlib.import_module("app.macro.fiscal_official").parse_government_bond_table(body, year)


def table(rows: str = "<tr><td>2026.01</td><td>123</td></tr>", *,
          title: str = TITLE, unit: str = UNIT,
          header: str = "<tr><th>月份</th><th>政府债券</th></tr>", width: int = 2) -> str:
    return (f'<table><tr><td colspan="{width}">{title}</td></tr>'
            f'<tr><td colspan="{width}">{unit}</td></tr>{header}{rows}</table>')


def document(content: str, charset: str = "utf-8", encoding: str = "utf-8") -> bytes:
    return f'<html><head><meta charset="{charset}"></head><body>{content}</body></html>'.encode(encoding)


def test_resolves_inserted_columns_and_merged_headers_without_differencing():
    header = ('<tr><th rowspan="2">月份 Month</th><th colspan="2">其中</th>'
              '<th rowspan="2">政 府<br>债券</th></tr>'
              '<tr><th>新增列</th><th>企业债券</th></tr>')
    rows = ('<tr><td>2026.03</td><td>999</td><td>888</td><td>1,234.500</td></tr>'
            '<tr><td>2026.01</td><td>777</td><td>666</td><td>-12.50</td></tr>'
            '<tr><td>2026.02</td><td>555</td><td>444</td><td>0</td></tr>')
    body = document(table(rows, header=header, width=4))
    assert parse(body) == {
        "year": 2026, "unit": "亿元", "rawSha256": hashlib.sha256(body).hexdigest(),
        "points": [{"period": "2026-01-01", "value": "-12.5"},
                   {"period": "2026-02-01", "value": "0"},
                   {"period": "2026-03-01", "value": "1234.5"}],
    }


def test_resolves_government_label_split_across_header_rows():
    header = ('<tr><th rowspan="2">月份</th><th>政府</th></tr>'
              '<tr><th>债券</th></tr>')
    assert parse(document(table(header=header)))["points"] == [{"period": "2026-01-01", "value": "123"}]


@pytest.mark.parametrize("charset,encoding", [("gb2312", "gb18030"), ("GB18030", "gb18030"),
                                               ("gbk", "gb18030"), ("UTF-8", "utf-8")])
def test_strict_declared_encoding_preserves_raw_hash(charset, encoding):
    body = document(table(title=TITLE + "𠀀"), charset, encoding)
    result = parse(body)
    assert result["points"] == [{"period": "2026-01-01", "value": "123"}]
    assert result["rawSha256"] == hashlib.sha256(body).hexdigest()


def test_excel_http_equiv_charset_and_entity_markup():
    text = table().replace("月份", "月&nbsp;份").replace("政府债券", "<span>政府</span>债券")
    body = (f'<meta http-equiv=Content-Type content="text/html; charset=gb2312">{text}').encode("gb18030")
    assert parse(body)["points"][0]["value"] == "123"


def test_utf8_bom_and_external_title_and_unit_apply_to_following_table():
    body = ('\ufeff<h1>' + TITLE + '</h1><p>' + UNIT + '</p><table>'
            '<tr><th>月份</th><th>政府债券</th></tr>'
            '<tr><td>2026.09</td><td>15362</td></tr></table>').encode("utf-8")
    assert parse(body)["points"] == [{"period": "2026-09-01", "value": "15362"}]


@pytest.mark.parametrize("value,want", [("-123", "-123"), ("+0.000", "0"), ("-0.00", "0"),
    ("1,234,567.8900", "1234567.89"), ("00012.0300", "12.03"),
    (" 123.450000000000000000000000000001 ", "123.450000000000000000000000000001")])
def test_decimal_values_are_exact_and_canonical(value, want):
    assert parse(document(table(f'<tr><td>2026.01</td><td>{value}</td></tr>')))["points"][0]["value"] == want


@pytest.mark.parametrize("missing", ["", " ", "&nbsp;", "-", "--", "—", "–", "…", "..."])
def test_missing_values_are_omitted_without_filling_months(missing):
    rows = f'<tr><td>2026.02</td><td>{missing}</td></tr><tr><td>2026.09</td><td>0</td></tr>'
    assert parse(document(table(rows)))["points"] == [{"period": "2026-09-01", "value": "0"}]


def test_all_unpublished_cells_return_no_points():
    assert parse(document(table('<tr><td>2026.12</td><td></td></tr>')))["points"] == []


def test_numbered_and_english_footer_continuations_after_twelve_months_are_not_data():
    rows = ''.join(f'<tr><td>2026.{m:02}</td><td>{m}</td></tr>' for m in range(1,13))
    rows += '<tr><td colspan="2">注：1.统计口径</td></tr><tr><td colspan="2">2.2019年12月调整政府债范围</td></tr><tr><td colspan="2">Since December 2019, the scope changed.</td></tr>'
    assert len(parse(document(table(rows)))["points"]) == 12


def test_note_section_does_not_hide_another_month_shaped_merged_row():
    rows = ''.join(f'<tr><td>2026.{m:02}</td><td>{m}</td></tr>' for m in range(1,13))
    rows += '<tr><td colspan="2">注：统计说明</td></tr><tr><td colspan="2">2026.01 123</td></tr>'
    with pytest.raises(ValueError): parse(document(table(rows)))


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e3", "1,23", "1,,234", "1234,567", "1 234",
    "12x", "12亿元", "1.2.3", ".", "+", "−12", "１２", "1,234.", "NULL", "N/A"])
def test_invalid_values_fail_instead_of_silently_disappearing(bad):
    with pytest.raises(ValueError):
        parse(document(table(f'<tr><td>2026.01</td><td>{bad}</td></tr>')))


@pytest.mark.parametrize("month", ["2026.00", "2026.13", "2026.1", "2026-01", "202601", "2026.01.01",
    "2025.12", "2027.01", "2026/01", "２０２６.01", "2026.0x", "2026.01*", "Jan", ""])
def test_bad_month_or_wrong_year_is_rejected(month):
    with pytest.raises(ValueError):
        parse(document(table(f'<tr><td>{month}</td><td>1</td></tr>')))


@pytest.mark.parametrize("values", [("1", "2"), ("", ""), ("—", "0")])
def test_duplicate_month_is_rejected_even_when_values_are_missing(values):
    rows = ''.join(f'<tr><td>2026.01</td><td>{v}</td></tr>' for v in values)
    with pytest.raises(ValueError):
        parse(document(table(rows)))


@pytest.mark.parametrize("year", [True, False, "2026", 2026.0, 0, -1, 10000])
def test_expected_year_is_a_real_four_digit_year(year):
    with pytest.raises(ValueError):
        parse(document(table()), year)


@pytest.mark.parametrize("title", ["社会融资规模存量统计表", "政府债券余额统计表", "", "其他统计表"])
def test_stock_or_other_title_is_not_flow(title):
    with pytest.raises(ValueError):
        parse(document(table(title=title)))


@pytest.mark.parametrize("unit", ["单位：万亿元人民币", "单位：亿元美元", "单位：亿元", "", "单位：万元人民币"])
def test_wrong_or_incomplete_currency_unit_is_rejected(unit):
    with pytest.raises(ValueError):
        parse(document(table(unit=unit)))


@pytest.mark.parametrize("header", [
    '<tr><th>月份</th><th>政府债券</th><th>政府债券</th></tr>',
    '<tr><th>月份</th><th colspan="2">政府债券</th></tr>',
    '<tr><th>月份</th><th>政府债券余额</th></tr>',
    '<tr><th>月份</th><th>企业债券</th></tr>',
    '<tr><th>月份</th><th>月份</th><th>政府债券</th></tr>',
])
def test_missing_or_ambiguous_semantic_column_is_rejected(header):
    with pytest.raises(ValueError):
        parse(document(table('<tr><td>2026.01</td><td>1</td><td>2</td></tr>', header=header, width=3)))


def test_two_candidate_tables_are_rejected():
    with pytest.raises(ValueError):
        parse(document(table() + table()))


def test_unrelated_table_does_not_change_selected_column():
    other = '<table><tr><td>其他数据</td><td>政府债券</td></tr></table>'
    assert parse(document(other + table()))["points"][0]["value"] == "123"


def test_title_and_unit_cannot_be_borrowed_from_another_table():
    other = table('', header='')
    with pytest.raises(ValueError):
        parse(document(other + table(title='', unit='')))


@pytest.mark.parametrize("rows", [
    '<tr><td>2026.01</td></tr>',
    '<tr><td>2026.01</td><td colspan="2">1</td></tr>',
    '<tr><td>2026.01</td><td rowspan="2">1</td></tr><tr><td>2026.02</td></tr>',
    '<tr><td rowspan="2">2026.01</td><td>1</td></tr><tr><td>2</td></tr>',
    '<tr><td></td><td>123</td></tr>',
])
def test_missing_or_merged_data_cells_fail_closed(rows):
    with pytest.raises(ValueError):
        parse(document(table(rows)))


@pytest.mark.parametrize("content", [
    '<table><tr><td><table></table></td></tr></table>',
    '<table><td>bad</td></table>', '<tr><td>bad</td></tr>',
    '<table><tr><td>bad</tr></table>', '<table><tr><td>bad</td></table>',
    '<table><tr><td>bad', '<table><tr><td>one<td>two</td></tr></table>',
    '<table><tr><td rowspan="2">A</td><td>B</td><td rowspan="2">C</td></tr>'
    '<tr><td colspan="2">overlap</td></tr></table>',
    '<table><tr><td rowspan="2">dangling span</td></tr></table>',
])
def test_malformed_nested_or_overlapping_table_is_rejected(content):
    with pytest.raises(ValueError):
        parse(document(content + table()))


@pytest.mark.parametrize("span", ["0", "65", "-1", "1.5", "many", "9999999999999999999999", ""])
@pytest.mark.parametrize("attr", ["rowspan", "colspan"])
def test_invalid_or_unbounded_span_is_rejected(attr, span):
    extra = f'<table><tr><td {attr}="{span}">x</td></tr></table>'
    with pytest.raises(ValueError):
        parse(document(extra + table()))


@pytest.mark.parametrize("extra", [
    '<table></table>' * 8,
    '<table>' + '<tr><td></td></tr>' * 257 + '</table>',
    '<table><tr>' + '<td></td>' * 65 + '</tr></table>',
    '<table><tr><td colspan="64"></td><td>x</td></tr></table>',
])
def test_table_row_and_column_limits_apply_to_all_tables(extra):
    with pytest.raises(ValueError):
        parse(document(extra + table()))


def test_inclusive_table_row_column_and_span_limits():
    extra = '<table>' + ('<tr><td colspan="64"></td></tr>' * 256) + '</table>'
    assert parse(document('<table></table>' * 6 + extra + table()))["points"][0]["value"] == "123"


def test_twelve_months_are_allowed_without_requiring_completeness():
    rows = ''.join(f'<tr><td>2026.{m:02}</td><td>{m}</td></tr>' for m in range(1, 13))
    assert len(parse(document(table(rows)))["points"]) == 12


@pytest.mark.parametrize('extra',['2026.1 123','2026-01 123','2026/01 123','2026.0x 123','2026年01月 123'])
def test_notes_cannot_hide_date_shaped_malformed_month(extra):
    rows=''.join(f'<tr><td>2026.{m:02}</td><td>{m}</td></tr>' for m in range(1,13))
    rows+='<tr><td colspan="2">注：统计说明</td></tr>'
    rows+=f'<tr><td colspan="2">{extra}</td></tr>'
    with pytest.raises(ValueError): parse(document(table(rows)))


def test_body_limit_is_inclusive_and_bound_to_original_bytes():
    raw = document(table())
    body = raw + b' ' * (5 * 1024 * 1024 - len(raw))
    assert parse(body)["rawSha256"] == hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError):
        parse(body + b' ')


@pytest.mark.parametrize("body", [b'', 'text', bytearray(b'html'), None,
    b'<meta charset="utf-8">\xff', b'<meta charset="gb2312">\x81',
    b'<meta charset="latin1">', b'<meta charset="utf-8"><meta charset="gb2312">',
    b'\xef\xbb\xbf<meta charset="gb2312">'])
def test_invalid_bytes_or_conflicting_encoding_is_rejected(body):
    with pytest.raises(ValueError):
        parse(body)


def test_script_and_style_text_cannot_supply_target_semantics():
    content = f'<script>{TITLE}</script><style>{UNIT}</style>' + table(title='', unit='')
    with pytest.raises(ValueError):
        parse(document(content))


@pytest.mark.parametrize("wrong_title", ["社会融资规模存量统计表", "政府债券余额统计表"])
def test_page_flow_title_cannot_override_local_stock_table_title(wrong_title):
    content = f'<h1>{TITLE}</h1>' + table(title=wrong_title)
    with pytest.raises(ValueError):
        parse(document(content))


@pytest.mark.parametrize("month,value", [("2026.01", "1<br>234"), ("2026.<br>01", "123")])
def test_html_line_breaks_cannot_join_invalid_numeric_or_month_tokens(month, value):
    with pytest.raises(ValueError):
        parse(document(table(f'<tr><td>{month}</td><td>{value}</td></tr>')))
