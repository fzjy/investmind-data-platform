"""从国家统计局公开发布目录发现月报；只接受已核对的指标/列/时间契约。"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from app.connectors.macro.contracts import (
    MacroFetchWindow, ProviderError, ProviderFetchResult, ProviderObservation, ProviderSchemaError,
)
from app.connectors.macro.html_contract import PublicHtmlContractParser
from app.connectors.macro.sanitization import sanitize_macro_text
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition

if TYPE_CHECKING:
    from app.connectors.macro.nbs import NbsTextTransport

INDEX = "https://www.stats.gov.cn/sj/zxfb/"
_RELEASE_PATH = re.compile(r"^/sj/zxfb/\d{6}/t\d{8}_\d+\.html$")
_PAGINATION = re.compile(r'createPageHTML\(\s*(\d+),\s*(\d+),\s*[\'\"]index[\'\"],\s*[\'\"]html[\'\"]\s*\)')
_MONTH = r"(?:[1-9]|1[0-2])"
_CPI_TITLE = re.compile(rf"^(\d{{4}})年({_MONTH})月份居民消费价格同比")
_CPI_ALTERNATE_TITLE = re.compile(
    rf"^(\d{{4}})年({_MONTH})月份居民消费价格(?P<basis>环比)?"
    r"(?P<change>(?:上涨|下降)\d+(?:\.\d+)?%|持平)$"
)
_INDUSTRY_TITLE = re.compile(rf"^(\d{{4}})年({_MONTH})月份规模以上工业增加值(?P<direction>增长|下降)(?P<value>\d+(?:\.\d+)?)%$")
_INDUSTRY_CUMULATIVE_TITLE = re.compile(r"^(\d{4})年1—([3-9]|1[0-2])月份规模以上工业增加值(?P<direction>增长|下降)(?P<value>\d+(?:\.\d+)?)%$")
_INDUSTRY_HALF_YEAR_TITLE = re.compile(r"^(\d{4})年上半年规模以上工业增加值(?P<direction>增长|下降)(?P<value>\d+(?:\.\d+)?)%$")
_INDUSTRY_TWO_YEAR_TITLE = re.compile(
    rf"^(2021)年({_MONTH})月份规模以上工业增加值(?P<direction>增长|下降)(?P<value>\d+(?:\.\d+)?)%"
    r"两年平均(?P<average_direction>增长|下降)(?P<average>\d+(?:\.\d+)?)%$"
)
_CONTRACTS = {
    "cn.cpi.core_yoy": ("不包括食品和能源", "同比涨跌幅（%）", _CPI_TITLE),
    "cn.industry.value_added_yoy": ("规模以上工业增加值", "同比实际增长（%）", _INDUSTRY_TITLE),
}


def _compact(value: str) -> str:
    return "".join(value.split())


def _period(match: re.Match[str]) -> date:
    try:
        return date(int(match[1]), 6 if match.re is _INDUSTRY_HALF_YEAR_TITLE else int(match[2]), 1)
    except ValueError as exc:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc


def _cpi_caption_periods(text: str) -> set[tuple[int, int]]:
    periods: set[tuple[int, int]] = set()
    for caption in re.findall(r"[^。，；！？]*居民消费价格主要数据[^。，；！？]*", text):
        # 先发现完整相关表题；缺年或带未知口径后缀不能变成不存在。
        monthly = re.fullmatch(rf"(\d{{4}})年({_MONTH})月(?:份)?居民消费价格主要数据", caption)
        if monthly is None or not monthly[1].isascii():
            periods.add((-1, -1))
        else:
            periods.add((int(monthly[1]), int(monthly[2])))
    return periods


def _half_year_caption_periods(text: str) -> set[tuple[int, int]]:
    periods: set[tuple[int, int]] = set()
    for year, period in re.findall(r"(?<!\d)(\d+)年([^。，；！？]*?)规模以上工业生产主要数据", text):
        periods.add((int(year), 6) if len(year) == 4 and year.isascii() and period == "6月份及上半年" else (-1, -1))
    return periods


def _monthly_industry_caption_periods(text: str) -> set[tuple[int, int]]:
    periods: set[tuple[int, int]] = set()
    for year, period in re.findall(r"(?<!\d)(\d+)年([^。，；！？]*?)规模以上工业生产主要数据", text):
        month = re.fullmatch(rf"({_MONTH})月份", period)
        periods.add((int(year), int(month[1])) if len(year) == 4 and year.isascii() and month else (-1, -1))
    return periods


class _ReleaseContractParser(PublicHtmlContractParser):
    def __init__(self) -> None:
        super().__init__()
        self.table_periods: list[set[tuple[int, int]]] = []
        self.half_year_table_periods: list[set[tuple[int, int]]] = []
        self.two_year_table_periods: list[set[tuple[int, int]]] = []
        self.cpi_table_periods: list[set[tuple[int, int]]] = []
        self.cpi_inside_periods: list[set[tuple[int, int]]] = []
        self.invalid_cpi_core_span = False
        # 保留原始跨度，仅已核对CPI/两年平均工业布局使用；不通用展开。
        self.table_spans: list[list[list[tuple[str | None, str | None]]]] = []
        self.raw_table_spans: list[list[list[tuple[str | None, str | None]]]] = []
        self._span_table: list[list[tuple[str | None, str | None]]] = []
        self._raw_span_table: list[list[tuple[str | None, str | None]]] = []
        self._span_row: list[tuple[str | None, str | None]] = []
        self._table_text_start = 0
        self._previous_table_end = 0
        self._table_count_before = 0
        self._pending_periods: set[tuple[int, int]] = set()
        self._pending_half_year_periods: set[tuple[int, int]] = set()
        self._pending_two_year_periods: set[tuple[int, int]] = set()
        self._pending_cpi_periods: set[tuple[int, int]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "table":
            self._span_table = []
            self._raw_span_table = []
            # 每张表单独关联前面的表题；正确桌面表题不能替移动表证明年份。
            before_table = _compact("".join(self.text_parts[self._previous_table_end:]))
            self._pending_periods = {(int(year), int(month)) for year, month in re.findall(
                rf"(?<!\d)(\d{{4}})年({_MONTH})月份规模以上工业生产主要数据", before_table)}
            self._pending_half_year_periods = _half_year_caption_periods(before_table)
            self._pending_two_year_periods = _monthly_industry_caption_periods(before_table)
            self._pending_cpi_periods = _cpi_caption_periods(before_table)
            self._table_count_before = len(self.tables)
            self._table_text_start = len(self.text_parts)
        elif tag.casefold() == "tr" and self._table is not None:
            self._span_row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            attributes = dict(attrs)
            self._span_row.append((attributes.get("rowspan", "1"), attributes.get("colspan", "1")))
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"td", "th"} and self._cell_parts is not None:
            # 在colspan归一化前保留核心身份，零跨度不能使坏副本消失。
            if (_compact("".join(self._cell_parts)).removeprefix("其中：") == "不包括食品和能源"
                    and self._span_row[-1] != ("1", "1")):
                self.invalid_cpi_core_span = True
        if tag.casefold() == "tr" and self._row:
            self._span_table.append(self._span_row)
        if tag.casefold() == "tr" and self._span_row:
            # 原始行独立保存；全零colspan不能使上游跨度或冲突总行消失。
            self._raw_span_table.append(self._span_row)
        super().handle_endtag(tag)
        # 保留块/表格单元的边界，避免上一单元的数字与下一段月份粘连。
        # 内联span不分隔，官方Word导出报告常将一句话拆成多个span。
        if tag.casefold() in {"p", "div", "table", "tr", "td", "th"}:
            self.text_parts.append("。")
        if tag.casefold() == "table":
            if len(self.tables) > self._table_count_before:
                self.table_spans.append(self._span_table)
                self.raw_table_spans.append(self._raw_span_table)
                self.table_periods.append(self._pending_periods)
                self.cpi_table_periods.append(self._pending_cpi_periods)
                inside_table = _compact("".join(self.text_parts[self._table_text_start:]))
                self.half_year_table_periods.append(self._pending_half_year_periods | _half_year_caption_periods(inside_table))
                self.two_year_table_periods.append(self._pending_two_year_periods | _monthly_industry_caption_periods(inside_table))
                self.cpi_inside_periods.append(_cpi_caption_periods(inside_table))
            self._previous_table_end = len(self.text_parts)


def _title_match(series_id: str, title: str) -> re.Match[str] | None:
    match = _CONTRACTS[series_id][2].match(title)
    if match is None and series_id == "cn.cpi.core_yoy":
        match = _CPI_ALTERNATE_TITLE.match(title)
    if match is None and series_id == "cn.industry.value_added_yoy":
        # 只兼容核实过的1—N月形式；1—2月合并发布仍不拆成单月。
        match = _INDUSTRY_CUMULATIVE_TITLE.match(title)
        if match is None:
            match = _INDUSTRY_HALF_YEAR_TITLE.match(title)
        if match is None:
            match = _INDUSTRY_TWO_YEAR_TITLE.match(title)
    return match


def _cpi_change_value(raw: str) -> Decimal:
    if raw == "持平":
        return Decimal("0")
    match = re.fullmatch(r"(上涨|下降)(\d+(?:\.\d+)?)%", raw)
    if match is None:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    return Decimal(match[2]) * (-1 if match[1] == "下降" else 1)


def _alternate_cpi_core_value(parser: _ReleaseContractParser, observed: date,
                              title_match: re.Match[str]) -> Decimal:
    # 新标题只在核心CPI路径使用；正文不能跨句猜值，所有副本证据均须一致。
    body_pattern = re.compile(
        rf"(?<![\d年—–-])(?:(?P<year>\d{{4}})年)?(?P<month>{_MONTH})月份，"
        r"(?:受[^，。；！？!?;,.]*，)?全国居民消费价格(?P<basis>同比|环比)(?P<change>[^，。；！？!?]*)"
    )
    body_values: dict[str, set[Decimal]] = {"同比": set(), "环比": set()}
    years: set[int] = set()
    for match in body_pattern.finditer(parser.compact_text):
        if int(match["month"]) != observed.month:
            raise ProviderSchemaError("NBS_VALUE_MISMATCH")
        if match["year"] is not None:
            years.add(int(match["year"]))
        body_values[match["basis"]].add(_cpi_change_value(match["change"]))
    if years != {observed.year} or any(len(values) != 1 for values in body_values.values()):
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    yoy, mom = next(iter(body_values["同比"])), next(iter(body_values["环比"]))
    if _cpi_change_value(title_match["change"]) != (mom if title_match["basis"] else yoy):
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")

    values: set[tuple[Decimal, ...]] = set()
    cumulative_values: dict[str, set[Decimal]] = {"total": set(), "core": set()}
    for table, before, inside in zip(parser.tables, parser.cpi_table_periods,
                                    parser.cpi_inside_periods, strict=True):
        core_rows = [row for row in table if row
                     and _compact(row[0]).removeprefix("其中：") == "不包括食品和能源"]
        if not core_rows:
            continue
        if before | inside != {(observed.year, observed.month)}:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        header = [_compact(cell) for cell in table[0]]
        base = ["", "环比涨跌幅（%）", "同比涨跌幅（%）"]
        if header not in (base, [*base, f"1—{observed.month}月同比涨跌幅（%）"]):
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        if any(any("涨跌幅" in _compact(cell) for cell in row) for row in table[1:]):
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        total_rows = [row for row in table if row and _compact(row[0]) == "居民消费价格"]
        if not total_rows:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for row in [*total_rows, *core_rows]:
            if len(row) != len(header):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            try:
                numbers = tuple(Decimal(_compact(cell)) for cell in row[1:])
            except InvalidOperation as exc:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
            if not all(value.is_finite() for value in numbers):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            pair = numbers[:2]
            if len(numbers) == 3:
                role = "total" if _compact(row[0]) == "居民消费价格" else "core"
                cumulative_values[role].add(numbers[2])
            if _compact(row[0]) == "居民消费价格":
                if pair != (mom, yoy):
                    raise ProviderSchemaError("NBS_VALUE_MISMATCH")
            else:
                values.add(numbers)
    if len(values) != 1 or any(len(evidence) > 1 for evidence in cumulative_values.values()):
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    return next(iter(values))[1]


def _legacy_cpi_core_value(parser: _ReleaseContractParser, observed: date,
                           headline: Decimal) -> Decimal | None:
    # 已核对的年度/季度及限定旧期布局；不靠同名列的出现顺序猜口径。
    dual_yoy_periods = {(2017, 7), (2017, 8), (2017, 9), (2017, 10), (2017, 11), (2018, 2)}
    known_layout = (observed.year, observed.month) in dual_yoy_periods | {(2017, 6), (2018, 4), (2018, 6)}
    if known_layout and parser.invalid_cpi_core_span:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    tables = [(table, before, inside, spans, raw_spans) for table, before, inside, spans, raw_spans in zip(
        parser.tables, parser.cpi_table_periods, parser.cpi_inside_periods, parser.table_spans, parser.raw_table_spans, strict=True)
        if any(any(_compact(cell).removeprefix("其中：") == "不包括食品和能源" for cell in row) for row in table)
        or (known_layout and (before or inside or any("居民消费价格" == _compact(cell) for row in table for cell in row)))]
    # 已取证期先严格分派，坏副本不能删重复同比头回落到普通列读取。
    legacy_signal = known_layout or any(
        any("涨跌" in _compact(cell) and "幅" not in _compact(cell) for cell in row)
        or sum(_compact(cell) == "同比涨跌幅（%）" for cell in row) > 1
        for table, _, _, _, _ in tables for row in table
    )
    if not legacy_signal:
        return None
    core_values: set[tuple[Decimal, ...]] = set()
    total_values: set[tuple[Decimal, ...]] = set()
    for table, before, inside, spans, raw_spans in tables:
        if before | inside != {(observed.year, observed.month)}:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        rows = [[_compact(cell) for cell in row] for row in table]
        if (observed.year, observed.month) in dual_yoy_periods:
            headers = [["", f"{observed.month}月", f"{observed.month}月", f"1-{observed.month}月"],
                       ["环比涨跌幅（%）", "同比涨跌幅（%）", "同比涨跌幅（%）"]]
            header_spans = [[("2", "1"), ("1", "2"), ("1", "1")], [("1", "1")] * 3]
        elif (observed.year, observed.month) == (2017, 6):
            headers = [["2017年6月居民消费价格主要数据"] * 4,
                       ["", "6月", "6月", "上半年"],
                       ["环比涨跌幅（%）", "同比涨跌幅（%）", "涨跌幅（%）"]]
            header_spans = [[("1", "4")], [("2", "1"), ("1", "2"), ("1", "1")], [("1", "1")] * 3]
        elif (observed.year, observed.month) in {(2018, 4), (2018, 6)}:
            headers = [[f"2018年{observed.month}月居民消费价格主要数据"] * 4,
                       ["", "环比涨跌幅（%）", "同比涨跌幅（%）", "上半年" if observed.month == 6 else "1-4月"],
                       ["同比涨跌幅（%）"]]
            header_spans = [[("1", "4")], [("2", "1")] * 3 + [("1", "1")], [("1", "1")]]
        elif observed.month == 12:
            headers = [["", "环比涨跌（%）", "同比涨跌（%）", f"{observed.year}年涨跌（%）"]]
            header_spans = [[("1", "1")] * 4]
        elif observed.month == 3:
            headers = [[f"{observed.year}年3月居民消费价格主要数据"] * 4,
                       ["", "3月", "3月", "一季度"],
                       ["环比涨跌幅（%）", "同比涨跌幅（%）", "同比涨跌幅（%）"]]
            header_spans = [[("1", "4")], [("2", "1"), ("1", "2"), ("1", "1")], [("1", "1")] * 3]
        else:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        if rows[:len(headers)] != headers or spans[:len(headers)] != header_spans:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        if known_layout and raw_spans[:len(headers)] != header_spans:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        data = rows[len(headers):]
        if known_layout and any(
                span != ("1", "1") for row in raw_spans[len(headers):] for span in row):
            # 已核对数据区无跨度；其他行也不能跨入核心行改变列位置。
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for row, row_spans in zip(data, spans[len(headers):], strict=True):
            if any(cell.removeprefix("其中：") in {"居民消费价格", "不包括食品和能源"} for cell in row):
                if row_spans != [("1", "1")] * 4:
                    raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        # 发现非法期别也要拒绝；不能只过滤合法月份后忽略13月/未知季度。
        period_or_basis = re.compile(r"(?:\d+[-—])?\d+月(?:份)?|(?:第)?[一二三四五六七八九十零\d]+季度|\d+年|(?:上|下)?半年|全年|同比|环比")
        if any(any("涨跌" in cell or period_or_basis.search(cell) for cell in row) for row in data):
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        total_rows = [row for row in data if "居民消费价格" in row]
        core_rows = [row for row in data if any(cell.removeprefix("其中：") == "不包括食品和能源" for cell in row)]
        if not total_rows or not core_rows:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for role, evidence in ((total_values, total_rows), (core_values, core_rows)):
            for row in evidence:
                aligned = (row[0] == "居民消费价格" if role is total_values
                           else row[0].removeprefix("其中：") == "不包括食品和能源")
                if len(row) != 4 or not aligned:
                    raise ProviderSchemaError("NBS_SCHEMA_INVALID")
                try:
                    numbers = tuple(Decimal(cell) for cell in row[1:])
                except InvalidOperation as exc:
                    raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
                if not all(value.is_finite() for value in numbers):
                    raise ProviderSchemaError("NBS_SCHEMA_INVALID")
                if role is total_values and numbers[1] != headline:
                    raise ProviderSchemaError("NBS_VALUE_MISMATCH")
                role.add(numbers)
    if len(core_values) != 1 or len(total_values) != 1:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    return next(iter(core_values))[1]


def _two_year_industry_monthly_value(parser: _ReleaseContractParser, observed: date,
                                     title: re.Match[str]) -> Decimal:
    def signed(direction: str, raw: str) -> Decimal:
        return Decimal(raw) * (-1 if direction == "下降" else 1)

    expected = signed(title["direction"], title["value"])
    average = signed(title["average_direction"], title["average"])
    monthly: set[tuple[int, Decimal, Decimal, Decimal]] = set()
    cumulative: set[tuple[int, Decimal, Decimal | None]] = set()
    # 先发现全部相关句，再验证完整句；不让正确副本掩盖错期/坏值。
    for period_text, raw in re.findall(
            r"([^。，；！？]*)，规模以上工业增加值同比([^。；！？]*)", parser.compact_text):
        period_match = re.fullmatch(r"(?:(\d+)年)?([\d—–-]+)月份", period_text)
        if period_match is None:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        year, period = period_match.groups()
        if year and (len(year) != 4 or int(year) != observed.year):
            raise ProviderSchemaError("NBS_VALUE_MISMATCH")
        if period == str(observed.month):
            match = re.fullmatch(
                r"实际(增长|下降)(\d+(?:\.\d+)?)%（以下增加值增速均为扣除价格因素的实际增长率），"
                r"比2019年同期(增长|下降)(\d+(?:\.\d+)?)%，两年平均(增长|下降)(\d+(?:\.\d+)?)%", raw)
            if match is None:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            monthly.add((observed.month, signed(match[1], match[2]), signed(match[5], match[6]), signed(match[3], match[4])))
        elif period == f"1—{observed.month}":
            match = re.fullmatch(r"(增长|下降)(\d+(?:\.\d+)?)%(?:，两年平均(增长|下降)(\d+(?:\.\d+)?)%)?", raw)
            if match is None:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            cumulative.add((observed.month, signed(match[1], match[2]), signed(match[3], match[4]) if match[3] else None))
        else:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    if len(monthly) != 1 or next(iter(monthly))[:3] != (observed.month, expected, average) or len(cumulative) != 1:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    cumulative_month, cumulative_value, _ = next(iter(cumulative))
    if cumulative_month != observed.month:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    found = False
    for table, periods, spans in zip(parser.tables, parser.two_year_table_periods, parser.table_spans, strict=True):
        rows = [(i, row) for i, row in enumerate(table) if any(_compact(c) == "规模以上工业增加值" for c in row)]
        if not rows:
            continue
        found = True
        if periods != {(observed.year, observed.month)} or len(table) < 3:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        headers = [[_compact(c) for c in row] for row in table[:2]]
        expected_headers = [["", f"{observed.month}月", f"{observed.month}月",
            f"1—{observed.month}月", f"1—{observed.month}月"],
            ["绝对量", "同比增长（%）", "绝对量", "同比增长（%）"]]
        if headers != expected_headers or spans[:2] != [[("2", "1"), ("1", "2"), ("1", "2")], [("1", "1")] * 4]:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for row in table[2:]:
            if any(re.fullmatch(r"(?:\d+[-—])?\d+月(?:份)?|\d+年|(?:第)?[一二三四五六七八九十零\d]+季度|(?:上|下)?半年|全年|同比|环比", _compact(c))
                   or re.search(r"(?:同期|两年平均|同比|环比).*（%）", _compact(c)) for c in row):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for index, row in rows:
            if index < 2 or len(row) != 5 or _compact(row[0]) != "规模以上工业增加值" or spans[index] != [("1", "1")] * 5:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            if [_compact(row[1]), _compact(row[3])] != ["…", "…"]:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            try:
                pair = (Decimal(_compact(row[2])), Decimal(_compact(row[4])))
            except InvalidOperation as exc:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
            if not all(value.is_finite() for value in pair):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            if pair != (expected, cumulative_value):
                raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    if not found:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    return expected


def _industry_body_values(text: str, *, cumulative: bool, require_actual: bool = False,
                          observed_year: int | None = None) -> set[tuple[int, Decimal]]:
    prefix = "1—" if cumulative else ""
    candidates = re.findall(rf"(?<![\d年—–-])(?:(\d{{4}})年)?{prefix}({_MONTH})月份，规模以上工业增加值同比([^。；，]*)", text)
    values: set[tuple[int, Decimal]] = set()
    has_actual = False
    for year, month, raw in candidates:
        if year and observed_year is not None and int(year) != observed_year:
            raise ProviderSchemaError("NBS_VALUE_MISMATCH")
        # 先发现相关证据，再校验值；NaN/--/缺百分号等不能被有效副本掩盖。
        match = re.fullmatch(r"(实际)?(增长|下降)(\d+(?:\.\d+)?)%(?:（[^）]*）)?", raw)
        if match is None or (require_actual and match[1] is None):
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        has_actual = has_actual or match[1] is not None
        values.add((int(month), Decimal(match[3]) * (-1 if match[2] == "下降" else 1)))
    if not values or (cumulative and not has_actual):
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    return values


def _half_year_industry_cumulative_value(text: str, observed: date) -> Decimal:
    values: set[Decimal] = set()
    for year, raw in re.findall(r"(?<![\d年])(?:(\d{4})年)?上半年，规模以上工业增加值同比([^。；，]*)", text):
        if year and int(year) != observed.year:
            raise ProviderSchemaError("NBS_VALUE_MISMATCH")
        match = re.fullmatch(r"(增长|下降)(\d+(?:\.\d+)?)%（增加值增速均为扣除价格因素的实际增长率）", raw)
        if match is None:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        values.add(Decimal(match[2]) * (-1 if match[1] == "下降" else 1))
    if len(values) != 1:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    return next(iter(values))


def _cumulative_industry_monthly_value(parser: _ReleaseContractParser, observed: date,
                                     cumulative_value: Decimal, *, half_year: bool = False) -> Decimal:
    text = parser.compact_text
    monthly_values = _industry_body_values(text, cumulative=False, require_actual=half_year,
        observed_year=observed.year if half_year else None)
    cumulative_values = ({(6, _half_year_industry_cumulative_value(text, observed))} if half_year
                         else _industry_body_values(text, cumulative=True))
    if half_year and re.search(r"(?<![\d年—–-])(?:\d{4}年)?1—(?:[1-9]|1[0-2])月份，规模以上工业增加值同比", text):
        cumulative_values |= _industry_body_values(text, cumulative=True, observed_year=observed.year)
    if (len(monthly_values) != 1 or next(iter(monthly_values))[0] != observed.month
            or cumulative_values != {(observed.month, cumulative_value)}):
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    monthly_value = next(iter(monthly_values))[1]
    table_values: set[tuple[Decimal, Decimal]] = set()
    periods_by_table = parser.half_year_table_periods if half_year else parser.table_periods
    for table, periods in zip(parser.tables, periods_by_table, strict=True):
        rows = [row for row in table if row and _compact(row[0]) == "规模以上工业增加值"]
        if not rows:
            continue
        if periods != {(observed.year, observed.month)}:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        # rowspan不由共享解析器展开；仅接受已核对5列或Word导出6列空尾。
        if len(table) < 3:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        months = [f"{observed.month}月", f"{observed.month}月",
                  f"1—{observed.month}月", f"1—{observed.month}月"]
        labels = ["绝对量", "同比增长（%）", "绝对量", "同比增长（%）"]
        top = [_compact(cell) for cell in table[0]]
        bottom = [_compact(cell) for cell in table[1]]
        standard = top == ["指标", *months] and bottom == labels
        blank_tail = top == ["", *months, ""] and bottom == [*labels, "同比增长（%）"]
        if not standard and not blank_tail:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        for row in rows:
            if len(row) != (6 if blank_tail else 5) or (blank_tail and _compact(row[-1]) != ""):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            try:
                pair = (Decimal(_compact(row[2])), Decimal(_compact(row[4])))
            except InvalidOperation as exc:
                raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
            if not all(value.is_finite() for value in pair):
                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
            table_values.add(pair)
    if not table_values:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    if table_values != {(monthly_value, cumulative_value)}:
        raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    return monthly_value


def _missing_windows(series_id: str, window: MacroFetchWindow, oldest: date | None,
                     observations: dict[date, ProviderObservation]) -> list[dict[str, str]]:
    if oldest is None or not observations:
        return [{"start": window.start.isoformat(), "end": window.end.isoformat()}]
    gaps: list[tuple[date, date]] = []
    if oldest > window.start:
        start, end = window.start, min(window.end, oldest - timedelta(days=1))
        if series_id == "cn.industry.value_added_yoy":
            # 预算前缀同样排除没有单月发布的 1—2 月；不能留下无法回填的纯空档。
            if start.month in (1, 2):
                start = start.replace(month=3, day=1)
            if end.month in (1, 2):
                end = date(end.year - 1, 12, 31)
        if start <= end:
            gaps.append((start, end))
    cursor = max(oldest, window.start.replace(day=1))
    if cursor < window.start:
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_visible = max(observations)
    while cursor <= last_visible:
        end = cursor.replace(day=monthrange(cursor.year, cursor.month)[1])
        # 工业 1—2 月只有合并发布，不制造不存在的单月观测或将其记作漏采。
        structurally_unpublished = series_id == "cn.industry.value_added_yoy" and cursor.month in (1, 2)
        if cursor not in observations and not structurally_unpublished:
            gaps.append((cursor, min(end, window.end)))
        cursor = (end + timedelta(days=1)).replace(day=1)
    merged: list[tuple[date, date]] = []
    for start, end in gaps:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [{"start": start.isoformat(), "end": end.isoformat()} for start, end in merged]


class _ReleaseLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: dict[str, str] = {}
        self._href: str | None = None
        self._title: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attributes = dict(attrs)
            self._href = attributes.get("href")
            self._title = attributes.get("title")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            url = urljoin(INDEX, self._href)
            # 桌面/移动端重复链接只保留一次，优先使用完整 title。
            title = _compact(self._title or "".join(self._text))
            if url not in self.links or self._title is not None:
                self.links[url] = title
            self._href = None


def _validate(series: MacroSeriesDefinition, source: MacroSourceDefinition) -> int:
    expected_series, expected_field, _ = _CONTRACTS[series.id]
    if str(source.source_url) != INDEX:
        raise ProviderSchemaError("NBS_PATH_NOT_ALLOWED")
    if (source.provider != "nbs" or source.series != expected_series or source.field != expected_field
            or source.access_mode != "public_page" or source.transform != "identity"
            or source.response_fields != ("ArticleTitle", "PubDate")
            or series.frequency != "monthly" or series.unit != "%"
            or set(source.params) != {"maxIndexPages"}):
        raise ProviderSchemaError("NBS_IDENTIFIER_INVALID")
    raw_limit = source.params["maxIndexPages"]
    if not raw_limit.isascii() or not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 1000:
        raise ProviderSchemaError("NBS_PAGE_LIMIT_INVALID")
    return int(raw_limit)


def _release_url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc == "www.stats.gov.cn"
            and _RELEASE_PATH.fullmatch(parts.path) is not None
            and not parts.query and not parts.fragment)


def _diagnostic_text(value: str | None) -> str | None:
    # 公开元数据先脱敏；最终失败上下文统一限长，不保存页面正文。
    return sanitize_macro_text(value) if value is not None else None


def _bounded_failure_context(context: dict[str, object]) -> dict[str, object]:
    truncated: dict[str, object] = {}

    def bound(value: object, path: str) -> object:
        if isinstance(value, str):
            safe = sanitize_macro_text(value)
            if len(safe) > 512:
                truncated[path] = {"characters": len(safe), "sha256": sha256(safe.encode()).hexdigest()}
            return safe[:512]
        if isinstance(value, dict):
            return {key: bound(item, f"{path}.{key}") for key, item in value.items()}
        return value

    # 只限制诊断副本；请求目标、分页及报告身份的校验仍使用原值。
    bounded = {key: bound(value, key) for key, value in context.items()}
    if truncated:
        bounded["truncatedFields"] = truncated
    return bounded


def _response_summary(body: str) -> dict[str, object]:
    # transport 返回解码文本；这是文本的 UTF-8 hash/长度，而非 HTTP 原始字节。
    # responseReceived 表示 get_text 返回文本；HTTP 错误也可能未返回文本。
    encoded = body.encode()
    return {"responseReceived": True, "textSha256": sha256(encoded).hexdigest(),
            "textBytes": len(encoded), "emptyBody": not body.strip(),
            "hasReplacementCharacters": "\ufffd" in body}


def _parse(series: MacroSeriesDefinition, source: MacroSourceDefinition, url: str, body: str,
           listed_title: str, diagnostics: dict[str, object] | None = None) -> ProviderObservation:
    from app.connectors.macro.nbs import _CHINA_STANDARD_TIME, _parse_release, _released_at

    parser = _ReleaseContractParser()
    parser.feed(body)
    if diagnostics is not None:
        diagnostics.update(articleTitle=_diagnostic_text(parser.meta.get("articletitle")),
                           pubDate=_diagnostic_text(parser.meta.get("pubdate")))
    title = _compact(parser.meta.get("articletitle", ""))
    if title != listed_title:
        raise ProviderSchemaError("NBS_TITLE_MISMATCH")
    match = _title_match(series.id, title)
    if match is None:
        raise ProviderSchemaError("NBS_SCHEMA_INVALID")
    observed = _period(match)
    released = _released_at(parser.meta.get("pubdate"))
    period_end = observed.replace(day=monthrange(observed.year, observed.month)[1])
    if released is None or released.astimezone(_CHINA_STANDARD_TIME).date() <= period_end:
        raise ProviderSchemaError("NBS_RELEASE_DATE_INVALID")
    if series.id == "cn.cpi.core_yoy":
        if match.re is _CPI_ALTERNATE_TITLE:
            value = _alternate_cpi_core_value(parser, observed, match)
            return ProviderObservation("nbs", str(source.series), observed, released, None, value, url)
        # 同时验证 CPI 标题与正文数据期，核心 CPI 不能误取 headline/环比/累计同比。
        headline, _ = _parse_release(source.model_copy(update={"source_url": url}), body)
        legacy_value = _legacy_cpi_core_value(parser, observed, headline.value)
        if legacy_value is not None:
            return ProviderObservation("nbs", str(source.series), observed, released, None, legacy_value, url)
        values: list[Decimal] = []
        for table, cpi_periods, inside_periods in zip(parser.tables, parser.cpi_table_periods,
                                                     parser.cpi_inside_periods, strict=True):
            for header_index, header in enumerate(table):
                columns = [i for i, label in enumerate(header) if _compact(label) == "同比涨跌幅（%）"]
                if len(columns) != 1:
                    continue
                column = columns[0]
                for row in table:
                    if row and _compact(row[0]).removeprefix("其中：") == "不包括食品和能源":
                        data_column = column
                        if len(row) == len(header) + 1:
                            # 旧表第一个空白指标格 rowspan=2；共享 parser 不展开
                            # rowspan，不能直接用第二层表头索引读成环比。
                            labels = [_compact(cell) for cell in header]
                            previous = [_compact(cell) for cell in table[header_index - 1]] if header_index else []
                            base_labels = ["环比涨跌幅（%）", "同比涨跌幅（%）"]
                            base_previous = ["", f"{observed.month}月", f"{observed.month}月"]
                            known_layout = (
                                (labels == base_labels and previous == base_previous)
                                or (labels == [*base_labels, "涨跌幅（%）"]
                                    and previous == [*base_previous, f"1-{observed.month}月"])
                                or (observed.month == 12 and labels == [*base_labels, "涨跌幅（%）"]
                                    and previous == [*base_previous, f"{observed.year}年"])
                            )
                            caption = [_compact(cell) for cell in table[header_index - 2]] if header_index >= 2 else []
                            expected_caption = f"{observed.year}年{observed.month}月居民消费价格主要数据"
                            caption_matches = (caption == [expected_caption] * len(row)
                                               and inside_periods == {(observed.year, observed.month)} if header_index >= 2
                                               else header_index == 1 and not inside_periods
                                               and cpi_periods == {(observed.year, observed.month)})
                            if not known_layout or not caption_matches:
                                raise ProviderSchemaError("NBS_SCHEMA_INVALID")
                            data_column += 1
                        elif len(row) != len(header):
                            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
                        try:
                            value = Decimal(_compact(row[data_column]))
                        except (IndexError, InvalidOperation) as exc:
                            raise ProviderSchemaError("NBS_SCHEMA_INVALID") from exc
                        if not value.is_finite():
                            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
                        values.append(value)
        if len(set(values)) != 1:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        value = values[0]
    else:
        if match.re is _INDUSTRY_TWO_YEAR_TITLE:
            value = _two_year_industry_monthly_value(parser, observed, match)
            return ProviderObservation("nbs", str(source.series), observed, released, None, value, url)
        value = Decimal(match["value"]) * (-1 if match["direction"] == "下降" else 1)
        if match.re in {_INDUSTRY_CUMULATIVE_TITLE, _INDUSTRY_HALF_YEAR_TITLE}:
            value = _cumulative_industry_monthly_value(parser, observed, value,
                half_year=match.re is _INDUSTRY_HALF_YEAR_TITLE)
            return ProviderObservation("nbs", str(source.series), observed, released, None, value, url)
        body_match = re.search(rf"(?<![\d—–-])({_MONTH})月份，规模以上工业增加值同比实际(增长|下降)(\d+(?:\.\d+)?)%", parser.compact_text)
        if body_match is None:
            raise ProviderSchemaError("NBS_SCHEMA_INVALID")
        body_value = Decimal(body_match[3]) * (-1 if body_match[2] == "下降" else 1)
        if int(body_match[1]) != observed.month or body_value != value:
            raise ProviderSchemaError("NBS_VALUE_MISMATCH")
    return ProviderObservation("nbs", str(source.series), observed, released, None, value, url)


def fetch_release_feed(transport: NbsTextTransport, series: MacroSeriesDefinition,
                       source: MacroSourceDefinition, window: MacroFetchWindow) -> ProviderFetchResult:
    requests = 0
    observations: dict[date, ProviderObservation] = {}
    raw: dict[str, object] = {"sourceUrl": str(source.source_url), "indexPages": [], "releases": []}
    failure_context: dict[str, object] | None = None
    try:
        limit = _validate(series, source)
        page_count = 1
        oldest: date | None = None
        seen: set[str] = set()
        pages: list[dict[str, object]] = []
        releases: list[dict[str, object]] = []
        raw.update(indexPages=pages, releases=releases)
        for page in range(limit):
            if page >= page_count:
                break
            page_url = INDEX if page == 0 else INDEX + f"index_{page}.html"
            failure_context = {"stage": "index", "url": page_url, "pageIndex": page,
                               "responseReceived": False}
            response = transport.get_text(page_url)
            requests += response.request_count
            failure_context.update(_response_summary(response.text))
            pagination = _PAGINATION.search(response.text)
            failure_context["pagination"] = (
                {"pageCount": pagination[1], "pageIndex": pagination[2]} if pagination else None
            )
            if pagination is None or int(pagination[2]) != page or not 1 <= int(pagination[1]) <= 1000:
                raise ProviderSchemaError("NBS_INDEX_SCHEMA_INVALID")
            page_count = int(pagination[1])
            parser = _ReleaseLinks()
            parser.feed(response.text)
            pages.append({"url": page_url, "sha256": failure_context["textSha256"]})
            index_context = failure_context.copy()
            for url, title in parser.links.items():
                # 上一篇报告的摘要不能冒充当前目录链接验证的失败响应。
                failure_context = index_context.copy()
                match = _title_match(series.id, title)
                if match is None or url in seen:
                    continue
                if not _release_url_allowed(url):
                    raise ProviderSchemaError("NBS_PATH_NOT_ALLOWED")
                seen.add(url)
                observed = _period(match)
                oldest = min(oldest, observed) if oldest is not None else observed
                if not window.start <= observed <= window.end:
                    continue
                failure_context = {"stage": "release", "url": url, "pageIndex": page,
                                   "responseReceived": False, "expectedTitle": _diagnostic_text(title)}
                article = transport.get_text(url)
                requests += article.request_count
                failure_context.update(_response_summary(article.text))
                observation = _parse(series, source, url, article.text, title, failure_context)
                if observation.released_at is not None and observation.released_at > window.as_of:
                    continue
                incumbent = observations.get(observed)
                if incumbent is not None and incumbent != observation:
                    raise ProviderSchemaError("NBS_DUPLICATE_PERIOD")
                observations[observed] = observation
                releases.append({"url": url, "articleTitle": title, "observedAt": observed.isoformat(),
                                 "releasedAt": observation.released_at.isoformat(), "value": str(observation.value),
                                 "sha256": failure_context["textSha256"]})
            if oldest is not None and oldest <= window.start:
                break
        # 不把有界目录扫描声称为十年完整回填；早于已扫描覆盖范围的窗口保持未解决。
        raw["missingWindows"] = _missing_windows(series.id, window, oldest, observations)
        return ProviderFetchResult("nbs", "success" if observations else "no_change",
                                   tuple(observations[d] for d in sorted(observations)), raw, requests)
    except ProviderError as exc:
        requests += exc.request_count
        raw["errorCode"] = exc.code
        if failure_context is not None:
            raw["failureContext"] = _bounded_failure_context(failure_context)
        return ProviderFetchResult("nbs", "failed", (), raw, requests,
                                   exc.sanitized_copy(request_count=requests))
