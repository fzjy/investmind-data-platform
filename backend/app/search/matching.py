import unicodedata
import re

from app.search.models import SearchOptions, SearchStockItem
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol


def normalize_query(raw: str, options: SearchOptions) -> str:
    query = unicodedata.normalize("NFKC", raw).strip()
    if len(query) > options.query_max_length:
        raise DataServiceError("INVALID_REQUEST", "搜索关键词过长")
    return query


def match_stocks(raw: object, query: str) -> list[SearchStockItem]:
    if not isinstance(raw, dict) or not isinstance(raw.get("bootstrap"), dict) or not isinstance(raw.get("stocks"), list):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约")
    seen: set[str] = set()
    matches: list[tuple[int, SearchStockItem]] = []
    key = query.casefold()
    for row in raw["stocks"]:
        if not isinstance(row, dict):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "股票搜索来源不符合契约")
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
        matches.append((rank, SearchStockItem(symbol=symbol, code=code, name=name)))
    return [item for rank, item in sorted(matches, key=lambda value: (value[0], value[1].symbol))]
