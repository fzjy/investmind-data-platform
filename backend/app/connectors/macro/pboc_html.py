from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re

from app.connectors.macro.contracts import ProviderSchemaError


@dataclass(frozen=True, slots=True)
class PbocTable:
    rows: list[list[str]]
    merged_cells: list[frozenset[int]]


class PbocTableParser(HTMLParser):
    """仅用于PBOC合同的合并单元格展开，不修改其他来源的HTML解析。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[PbocTable] = []
        self._table: list[list[str]] | None = None
        self._table_merged: list[frozenset[int]] = []
        self._row: list[str] | None = None
        self._row_merged: set[int] = set()
        self._cell: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1
        self._pending: dict[int, tuple[int, str]] = {}
        self._remaining: dict[int, tuple[int, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "table":
            if self._table is not None:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            self._table = []
            self._table_merged = []
        elif tag == "tr" and self._table is not None:
            if self._row is not None:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            self._row = []
            self._row_merged = set()
            self._remaining, self._pending = self._pending, {}
            self._consume_rowspans()
        elif tag in {"td", "th"} and self._row is not None:
            if self._cell is not None:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            attributes = {key.casefold(): value for key, value in attrs}
            self._colspan = _span(attributes, "colspan")
            self._rowspan = _span(attributes, "rowspan")
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            value = " ".join("".join(self._cell).split())
            start = len(self._row)
            columns = range(start, start + self._colspan)
            if any(column in self._remaining for column in columns):
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            self._row.extend([value] * self._colspan)
            if self._colspan > 1 or self._rowspan > 1:
                self._row_merged.update(columns)
            if self._rowspan > 1:
                for column in columns:
                    self._pending[column] = (self._rowspan - 1, value)
            self._cell = None
            self._consume_rowspans()
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            self._consume_rowspans()
            if self._remaining:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            assert self._table is not None
            self._table.append(self._row)
            self._table_merged.append(frozenset(self._row_merged))
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._row is not None or self._pending:
                raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
            self.tables.append(PbocTable(self._table, self._table_merged))
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        # HTMLParser.close会把残缺标签当文本消费；先拒绝尚未消费的尾部。
        if self.rawdata.strip():
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
        super().close()
        if self._table is not None or self._row is not None or self._cell is not None:
            raise ProviderSchemaError("PBOC_SCHEMA_INVALID")

    def _consume_rowspans(self) -> None:
        assert self._row is not None
        while len(self._row) in self._remaining:
            column = len(self._row)
            remaining, value = self._remaining.pop(column)
            self._row.append(value)
            self._row_merged.add(column)
            if remaining > 1:
                self._pending[column] = (remaining - 1, value)


def _span(attributes: dict[str, str | None], name: str) -> int:
    if name not in attributes:
        return 1
    raw = attributes[name]
    # 年表最多25列；先限制字符串长度，避免异常span造成巨大分配。
    if raw is None or re.fullmatch(r"[0-9]{1,2}", raw.strip()) is None:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    value = int(raw)
    if not 1 <= value <= 25:
        raise ProviderSchemaError("PBOC_SCHEMA_INVALID")
    return value
