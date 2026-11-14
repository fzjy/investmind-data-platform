from __future__ import annotations

import re
from collections.abc import Iterable

from app.shared.errors import DataServiceError, ErrorDetail


_STOCK_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def require_object(
    value: object,
    required_keys: Iterable[str],
    *,
    message: str = "数据源响应不符合契约",
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", message)
    missing = [key for key in required_keys if key not in value]
    if missing:
        details: list[ErrorDetail] = [
            {"field": key, "message": "数据源字段缺失"} for key in missing
        ]
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", message, details)
    return dict(value)


def normalize_stock_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not _STOCK_SYMBOL.fullmatch(symbol):
        raise DataServiceError(
            "INVALID_REQUEST",
            "股票代码不合法",
            [{"field": "symbol", "message": "必须为六位代码并使用 SH、SZ 或 BJ 后缀"}],
        )
    return symbol
