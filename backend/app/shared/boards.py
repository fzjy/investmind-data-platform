from __future__ import annotations

import re


_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")
_LABELS = {"科创板": "star", "创业板": "chinext", "北交所": "bse"}
_EXCHANGES = {"SSE": "sh-main", "SZSE": "sz-main", "BSE": "bse"}
_SUFFIXES = {"SH": "sh-main", "SZ": "sz-main", "BJ": "bse"}


def normalize_board(
    value: object, *, exchange: object = None, symbol: object = None,
) -> str | None:
    """统一供应商标签和历史页面字段；无法识别时不猜测上市板块。"""
    if value is None:
        # 北交所与上市板块一一对应；沪深交易所不能区分主板与科技板块。
        return "bse" if exchange == "BSE" else None
    if isinstance(value, str) and value not in {"main", "主板"}:
        return _LABELS.get(value, value)
    if value is not None and not isinstance(value, str):
        return None
    if isinstance(exchange, str) and exchange in _EXCHANGES:
        return _EXCHANGES[exchange]
    if value is not None and isinstance(symbol, str):
        match = _SYMBOL.fullmatch(symbol)
        if match:
            return _SUFFIXES[match.group(1)]
    return value
