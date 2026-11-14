"""央行社融增量表中的政府债券月值；仅解析原字节，不做 IO 或月值差分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
import re


_TITLE = "社会融资规模增量统计表"
_MAX_BODY = 5 * 1024 * 1024
_MAX_TABLES = 8
_MAX_ROWS = 256
_MAX_COLUMNS = 64
_MAX_SPAN = 64
_MISSING = frozenset(("", "-", "--", "—", "–", "…", "..."))
_MONTH = re.compile(r"([0-9]{4})\.([0-9]{2})")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")


def _compact(text: str) -> str:
    return "".join(text.split())


class _EncodingParser(HTMLParser):
    """Latin-1 仅用于读取 ASCII meta 声明，绝不当作正文解码结果。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.encodings: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        for name, value in attrs:
            if value is None:
                continue
            if name == "charset":
                self.encodings.add(value.strip().lower())
            elif name == "content":
                for match in re.finditer(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", value, re.I):
                    self.encodings.add(match[1].lower())


def _decode(body: bytes) -> str:
    declarations = _EncodingParser()
    declarations.feed(body.decode("latin-1"))
    declarations.close()
    aliases = {"gb2312": "gb18030", "gbk": "gb18030", "gb18030": "gb18030",
               "utf-8": "utf-8", "utf8": "utf-8"}
    if declarations.encodings - aliases.keys():
        raise ValueError("Unsupported HTML encoding")
    codecs = {aliases[name] for name in declarations.encodings}
    if body.startswith(b"\xef\xbb\xbf"):
        codecs.add("utf-8")
    if len(codecs) > 1:
        raise ValueError("Conflicting HTML encodings")
    # 未声明时也只接受严格 UTF-8；不做编码试探或 replacement 解码。
    encoding = next(iter(codecs), "utf-8")
    try:
        return body.decode("utf-8-sig" if encoding == "utf-8" else encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("HTML bytes do not match declared encoding") from exc


@dataclass
class _Cell:
    row: int
    column: int
    rowspan: int
    colspan: int
    tag: str
    chunks: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.chunks).strip()


@dataclass
class _Table:
    prefix: list[str]
    rows: list[list[_Cell]] = field(default_factory=list)
    grid: dict[tuple[int, int], _Cell] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)


def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
    values = [value for key, value in attrs if key == name]
    if not values:
        return 1
    value = values[0]
    if len(values) != 1 or value is None or re.fullmatch(r"[0-9]{1,3}", value) is None:
        raise ValueError("Invalid table span")
    result = int(value)
    if not 1 <= result <= _MAX_SPAN:
        raise ValueError("Table span exceeds limit")
    return result


class _TableParser(HTMLParser):
    """有界展开合并单元格；坏结构不交给浏览器式容错猜测。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.context: list[str] = []
        self.table: _Table | None = None
        self.row: list[_Cell] | None = None
        self.cell: _Cell | None = None
        self.column = 0
        self.ignored: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.ignored is not None:
            return
        if tag in {"script", "style"}:
            self.ignored = tag
            return
        if tag == "table":
            if self.table is not None:
                raise ValueError("Nested tables are forbidden")
            if len(self.tables) >= _MAX_TABLES:
                raise ValueError("Too many tables")
            self.table = _Table(self.context)
            self.context = []
            self.tables.append(self.table)
        elif tag == "tr":
            if self.table is None or self.row is not None:
                raise ValueError("Invalid table row structure")
            if len(self.table.rows) >= _MAX_ROWS:
                raise ValueError("Too many table rows")
            self.row = []
            self.table.rows.append(self.row)
            self.column = 0
        elif tag in {"td", "th"}:
            self._start_cell(tag, attrs)
        elif tag == "br" and self.cell is not None:
            # 表头可跨行组合；数据格则保留分隔，不能把两行数字拼成新数值。
            self.cell.chunks.append("\n")

    def _start_cell(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.table is None or self.row is None or self.cell is not None:
            raise ValueError("Invalid table cell structure")
        row = len(self.table.rows) - 1
        while (row, self.column) in self.table.grid:
            self.column += 1
        height, width = _span(attrs, "rowspan"), _span(attrs, "colspan")
        if self.column + width > _MAX_COLUMNS or row + height > _MAX_ROWS:
            raise ValueError("Expanded table exceeds row/column limit")
        cell = _Cell(row, self.column, height, width, tag)
        for r in range(row, row + height):
            for c in range(self.column, self.column + width):
                if (r, c) in self.table.grid:
                    raise ValueError("Overlapping merged cells")
                self.table.grid[r, c] = cell
        self.row.append(cell)
        self.cell = cell
        self.column += width

    def handle_endtag(self, tag: str) -> None:
        if self.ignored is not None:
            if tag == self.ignored:
                self.ignored = None
            return
        if tag in {"td", "th"}:
            if self.cell is None or self.cell.tag != tag:
                raise ValueError("Unmatched table cell close")
            self.cell = None
        elif tag == "tr":
            if self.row is None or self.cell is not None:
                raise ValueError("Unclosed table cell or unmatched row")
            self.row = None
        elif tag == "table":
            if self.table is None or self.row is not None or self.cell is not None:
                raise ValueError("Unclosed table row or unmatched table")
            if any(r >= len(self.table.rows) for r, _ in self.table.grid):
                raise ValueError("Rowspan extends beyond table rows")
            self.table = None

    def handle_data(self, data: str) -> None:
        if self.ignored is not None:
            return
        if self.cell is not None:
            self.cell.chunks.append(data)
        elif data.strip():
            (self.context if self.table is None else self.table.extra).append(data)

    def finish(self) -> None:
        self.close()
        if self.table is not None or self.row is not None or self.cell is not None:
            raise ValueError("Unclosed table structure")


def _texts(table: _Table) -> list[str]:
    return table.prefix + table.extra + [cell.text for row in table.rows for cell in row]


def _month_header(cell: _Cell) -> bool:
    text = _compact(cell.text)
    return "月份" in text or text.lower() == "month"


def _columns(table: _Table) -> tuple[int, int, int]:
    months = [cell for row in table.rows for cell in row if _month_header(cell)]
    month_columns = {c for cell in months for c in range(cell.column, cell.column + cell.colspan)}
    if len(month_columns) != 1:
        raise ValueError("Expected one unambiguous month column")
    month = next(iter(month_columns))
    header_end = max(cell.row + cell.rowspan for cell in months)
    government_columns: set[int] = set()
    for c in range(_MAX_COLUMNS):
        path: list[str] = []
        previous: _Cell | None = None
        for r in range(header_end):
            cell = table.grid.get((r, c))
            if cell is None or cell is previous:
                continue
            previous = cell
            text = _compact(cell.text)
            if text == "政府债券":
                government_columns.add(c)
            if cell.colspan == 1 and text:
                path.append(text)
        if any(left + right == "政府债券" for left, right in zip(path, path[1:])):
            government_columns.add(c)
    if len(government_columns) != 1 or month in government_columns:
        raise ValueError("Expected one unambiguous government bond column")
    government = next(iter(government_columns))
    local_metadata = [cell.text for row in table.rows[:header_end] for cell in row]
    local_titles = [text for text in local_metadata if "统计表" in _compact(text)]
    if local_titles and any(_TITLE not in _compact(text) for text in local_titles):
        raise ValueError("Local table title is not the flow table")
    metadata = table.prefix + table.extra + local_metadata
    if not any(_TITLE in _compact(text) for text in metadata):
        raise ValueError("Missing flow table title")
    units = [_compact(text) for text in metadata if "单位" in _compact(text)]
    if not units or any(re.fullmatch(r"单位[:：]?亿元人民币", unit) is None for unit in units):
        raise ValueError("Expected unit: 100 million RMB")
    return month, government, header_end


def _value(text: str) -> str | None:
    if text in _MISSING:
        return None
    if _NUMBER.fullmatch(text) is None:
        raise ValueError("Invalid government bond decimal")
    value = Decimal(text.replace(",", ""))
    if value == 0:
        return "0"
    # 不用 normalize()，避免 Decimal context 将高精度原值舍入。
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _points(table: _Table, year: int) -> list[dict[str, str]]:
    month, government, header_end = _columns(table)
    seen: set[int] = set()
    points: list[dict[str, str]] = []
    notes_started = False
    for r in range(header_end, len(table.rows)):
        row = table.rows[r]
        if not any(cell.text for cell in row):
            continue
        period_cell = table.grid.get((r, month))
        value_cell = table.grid.get((r, government))
        # Excel 原件尾部有合并整行的中英文说明及排版空行。
        if (period_cell is not None and period_cell.colspan > 1
                and period_cell.column <= month and period_cell.column + period_cell.colspan > government
                and all(cell is period_cell or not cell.text for cell in row)):
            if re.match(r"(?:注[:：]|AFRE\(flow\))", period_cell.text):
                notes_started = True
                continue
            if notes_started and len(seen) == 12 and not re.match(r"[0-9]{4}(?:[./年-]|\s|$)", period_cell.text):
                continue
        if period_cell is None or value_cell is None:
            raise ValueError("Missing month or government bond cell")
        if any(cell.rowspan != 1 or cell.colspan != 1 for cell in (period_cell, value_cell)):
            raise ValueError("Merged monthly data cells are ambiguous")
        match = _MONTH.fullmatch(period_cell.text)
        if match is None or int(match[1]) != year or not 1 <= int(match[2]) <= 12:
            raise ValueError("Invalid month or unexpected year")
        number = int(match[2])
        if number in seen:
            raise ValueError("Duplicate month")
        seen.add(number)
        value = _value(value_cell.text)
        if value is not None:
            points.append({"period": f"{year:04}-{number:02}-01", "value": value})
    if not seen:
        raise ValueError("No monthly rows")
    return sorted(points, key=lambda point: point["period"])


def parse_government_bond_table(body: bytes, expected_year: int) -> dict:
    """返回 year/unit/rawSha256/points；缺值省略，坏输入统一安全失败。"""
    if not isinstance(body, bytes) or not body or len(body) > _MAX_BODY:
        raise ValueError("Expected nonempty HTML bytes up to 5 MiB")
    if type(expected_year) is not int or not 1000 <= expected_year <= 9999:
        raise ValueError("Expected a four-digit integer year")
    parser = _TableParser()
    parser.feed(_decode(body))
    parser.finish()
    candidates = [table for table in parser.tables if any(_TITLE in _compact(text) for text in _texts(table))]
    if len(candidates) != 1:
        raise ValueError("Expected one flow table candidate")
    return {"year": expected_year, "unit": "亿元", "rawSha256": sha256(body).hexdigest(),
            "points": _points(candidates[0], expected_year)}
