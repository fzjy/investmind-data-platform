from __future__ import annotations

from html.parser import HTMLParser


class PublicHtmlContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.text_parts: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_colspan = 1

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.casefold(): value for name, value in attrs if value is not None}
        if tag.casefold() == "meta":
            name = attributes.get("name")
            content = attributes.get("content")
            if name is not None and content is not None:
                self.meta[name.casefold()] = content.strip()
        elif tag.casefold() == "table":
            self._table = []
        elif tag.casefold() == "tr" and self._table is not None:
            self._row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            raw_colspan = attributes.get("colspan", "1")
            self._cell_colspan = int(raw_colspan) if raw_colspan.isdigit() else 1
        elif tag.casefold() == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in {"td", "th"} and self._cell_parts is not None:
            if self._row is not None:
                value = _normalized_text("".join(self._cell_parts))
                self._row.extend([value] * self._cell_colspan)
            self._cell_parts = None
            self._cell_colspan = 1
        elif normalized_tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif normalized_tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    @property
    def compact_text(self) -> str:
        return "".join(self.text_parts).replace("\u3000", "").replace("\xa0", "").replace(" ", "").replace("\r", "").replace("\n", "").replace("\t", "")


def _normalized_text(value: str) -> str:
    return " ".join(value.replace("\u3000", " ").replace("\xa0", " ").split())
