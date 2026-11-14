"""页面与结构化股票记录的发布前一致性检查，不依赖数据库约束兜底。"""
from collections.abc import Mapping, Sequence
from datetime import date, datetime
import re

from app.marts.contracts import MartCandidate, QualityResult


_SYMBOL = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_CANDLE_FIELDS = ("open", "high", "low", "close", "volume")


def _same(left: object, right: object) -> bool:
    # Python 的 True == 1 不能代表持久化字段一致；递归保留缺键与 null 的区别。
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    try:
        return left == right
    except (ArithmeticError, TypeError, ValueError):
        return False


def _symbol(row: Mapping[str, object]) -> str | None:
    symbol = row.get("symbol")
    return symbol if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol) else None


def _chart_date(row: Mapping[str, object]) -> str | None:
    value = row.get("date") or row.get("tradeDate") or row.get("trade_date")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return None


def _index(rows, key_fn, side, report):
    indexed = {}
    if not isinstance(rows, (list, tuple)):
        report({"problem": "invalid-collection", "side": side})
        return indexed
    for position, row in enumerate(rows):
        if isinstance(row, Mapping) and any(not isinstance(field, str) for field in row):
            # JSON 会把整数键转换为字符串，不能因此把损坏字段名视作正常对应字段。
            report({"problem": "invalid-field-name", "side": side, "index": position})
            continue
        key = key_fn(row) if isinstance(row, Mapping) else None
        if key is None:
            report({"problem": "invalid-key", "side": side, "index": position})
        elif key in indexed:
            report({"problem": "duplicate-key", "side": side, "key": key})
        else:
            indexed[key] = row
    return indexed


def _compare(payload_rows, structured_rows, key_fn, report, fields=None):
    left = _index(payload_rows, key_fn, "payload", report)
    right = _index(structured_rows, key_fn, "rows", report)
    if isinstance(payload_rows, (list, tuple)) and isinstance(structured_rows, (list, tuple)):
        if len(payload_rows) != len(structured_rows):
            report({"problem": "count", "payloadCount": len(payload_rows), "rowsCount": len(structured_rows)})
    if left.keys() != right.keys():
        report({"problem": "key-set", "onlyPayload": sorted(left.keys() - right.keys()),
                "onlyRows": sorted(right.keys() - left.keys())})
    for key in sorted(left.keys() & right.keys()):
        a, b = left[key], right[key]
        compared_fields = fields if fields is not None else sorted(a.keys() | b.keys())
        mismatched = [field for field in compared_fields
                      if field not in a or field not in b or not _same(a[field], b[field])]
        if mismatched:
            # 只记录位置/字段名，不把 NaN、Decimal 或整份业务 payload 放进 JSON 详情。
            report({"problem": "fields", "key": key, "fields": mismatched})


def row_consistency(candidates: Sequence[MartCandidate]) -> QualityResult:
    issues: list[dict[str, object]] = []
    for candidate in candidates:
        base, _, symbol = candidate.product.partition(":")
        if base not in {"screener", "stock-workbench"}:
            continue

        def report(collection, problem):
            issues.append({"product": candidate.product, "collection": collection, **problem})

        if base == "screener":
            _compare(candidate.payload.get("stocks"), candidate.rows.screener, _symbol,
                     lambda problem: report("screener", problem))
            continue

        quote = candidate.payload.get("quote")
        row_symbols = [_symbol(row) if isinstance(row, Mapping) else None
                       for row in candidate.rows.workbenches]
        if (not _SYMBOL.fullmatch(symbol) or candidate.payload.get("symbol") != symbol
                or not isinstance(quote, Mapping) or quote.get("symbol") != symbol
                or row_symbols != [symbol]):
            report("workbenches", {"problem": "identity"})
        _compare([candidate.payload], candidate.rows.workbenches, _symbol,
                 lambda problem: report("workbenches", problem))
        chart = candidate.payload.get("dailyChart")
        candles = chart.get("candles") if isinstance(chart, Mapping) else None
        _compare(candles, candidate.rows.charts, _chart_date,
                 lambda problem: report("charts", problem), _CANDLE_FIELDS)
    return QualityResult("payload-rows-consistent", "blocking", not issues, {"issues": issues})
