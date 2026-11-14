import unicodedata
import re

from app.search.models import SearchOptions, SearchPageItem, SearchStockItem
from app.search.page_catalog import PAGE_CATALOG
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol
from app.stocks.api.quotes import normalize_screener_universe


def normalize_query(raw: str, options: SearchOptions) -> str:
    query = unicodedata.normalize("NFKC", raw).strip()
    if len(query) > options.query_max_length:
        raise DataServiceError("INVALID_REQUEST", "搜索关键词过长")
    return query


def match_stocks(raw: object, query: str) -> list[SearchStockItem]:
    rows = normalize_screener_universe(raw)["stocks"]
    seen: set[str] = set()
    matches: list[tuple[int, SearchStockItem]] = []
    key = query.casefold()
    for row in rows:
        symbol, name = row.get("symbol"), row.get("name")
        if not isinstance(symbol, str) or not isinstance(name, str) or not name.strip():
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约")
        try:
            symbol = normalize_stock_symbol(symbol)
        except DataServiceError:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约") from None
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol) is None:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约")
        if symbol in seen:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约")
        seen.add(symbol)
        name = name.strip()
        code = symbol[:6]
        codes = (code.casefold(), symbol.casefold())
        normalized_name = unicodedata.normalize("NFKC", name).casefold()
        if key in codes:
            rank = 0
        elif key == normalized_name:
            rank = 1
        elif any(value.startswith(key) for value in (*codes, normalized_name)):
            rank = 2
        elif any(key in value for value in (*codes, normalized_name)):
            rank = 3
        else:
            continue
        matches.append((rank, SearchStockItem(symbol=symbol, code=code, name=name, href="/stocks/" + symbol)))
    return [item for rank, item in sorted(matches, key=lambda value: (value[0], value[1].symbol))]


def match_pages(query: str) -> list[SearchPageItem]:
    key = query.casefold()
    matches = []
    for entry in PAGE_CATALOG:
        candidates = (entry.title.casefold(), *(word.casefold() for word in entry.keywords))
        if not key:
            rank = 0
        elif key == candidates[0]:
            rank = 0
        elif any(value.startswith(key) for value in candidates):
            rank = 1
        elif any(key in value for value in candidates):
            rank = 2
        else:
            continue
        matches.append((rank, entry))
    return [SearchPageItem(id=entry.id, title=entry.title, description=entry.description, href=entry.href)
            for rank, entry in sorted(matches, key=lambda value: (value[0], value[1].id))]
