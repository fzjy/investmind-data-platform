from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any


def parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return datetime.fromisoformat(text).date()


def decimal_value(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _coerce(value: object) -> object:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        numeric = Decimal(str(value))
        return int(numeric) if numeric == numeric.to_integral_value() else float(numeric)
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return value
    try:
        numeric = Decimal(text)
        return int(numeric) if numeric == numeric.to_integral_value() else float(numeric)
    except InvalidOperation:
        return value


def normalize_security(row: dict[str, object]) -> dict[str, object]:
    return {
        "ts_code": str(row["ts_code"]),
        "symbol": str(row.get("symbol") or row["ts_code"]).split(".")[0],
        "name": str(row.get("name") or row["ts_code"]),
        "exchange": row.get("exchange"),
        "board": row.get("market"),
        "list_date": parse_date(row.get("list_date")),
        "status": row.get("list_status"),
        "is_st": str(row.get("name") or "").upper().startswith(("ST", "*ST")),
        "source": "tushare",
        "data": {key: value for key, value in row.items()},
    }


def normalize_daily_price(row: dict[str, object]) -> dict[str, object]:
    keys = ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount")
    result: dict[str, object] = {
        "ts_code": str(row["ts_code"]),
        "trade_date": parse_date(row["trade_date"]),
    }
    result.update({key: decimal_value(row.get(key)) for key in keys})
    result["data"] = {key: _coerce(value) for key, value in row.items()}
    return result


def normalize_daily_basic(row: dict[str, object]) -> dict[str, object]:
    keys = ("turnover_rate", "pe", "pb", "ps", "total_mv", "circ_mv", "dv_ratio", "dv_ttm")
    result: dict[str, object] = {
        "ts_code": str(row["ts_code"]),
        "trade_date": parse_date(row["trade_date"]),
    }
    result.update({key: decimal_value(row.get(key)) for key in keys})
    result["data"] = {key: _coerce(value) for key, value in row.items()}
    return result


def normalize_financial_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "ts_code": str(row["ts_code"]),
        "ann_date": parse_date(row.get("ann_date")),
        "f_ann_date": parse_date(row.get("f_ann_date")),
        "end_date": parse_date(row["end_date"]),
        "report_type": row.get("report_type"),
        "comp_type": row.get("comp_type"),
        "update_flag": row.get("update_flag"),
        "data": {key: _coerce(value) for key, value in row.items()},
    }


def normalize_trade_calendar(row: dict[str, object]) -> dict[str, object]:
    from app.shared.calendar import open_flag
    return {"exchange": str(row["exchange"]), "cal_date": parse_date(row["cal_date"]), "is_open": open_flag(row.get("is_open")), "pretrade_date": parse_date(row.get("pretrade_date"))}


def normalize_disclosure_schedule(row: dict[str, object]) -> dict[str, object]:
    return {
        "ts_code": str(row["ts_code"]),
        "ann_date": parse_date(row.get("ann_date")),
        "end_date": parse_date(row["end_date"]),
        "pre_date": parse_date(row.get("pre_date")),
        "actual_date": parse_date(row.get("actual_date")),
        "modify_date": parse_date(row.get("modify_date")),
        "data": {key: value for key, value in row.items()},
    }


def normalize_adjustment_factor(row: dict[str, object]) -> dict[str, object]:
    return {"ts_code": str(row["ts_code"]), "trade_date": parse_date(row["trade_date"]), "adj_factor": decimal_value(row.get("adj_factor"))}


def normalize_dividend(row: dict[str, object]) -> dict[str, object]:
    return {"ts_code": str(row["ts_code"]), "ann_date": parse_date(row.get("ann_date")), "end_date": parse_date(row.get("end_date")), "div_proc": row.get("div_proc"), "cash_div": decimal_value(row.get("cash_div")), "cash_div_tax": decimal_value(row.get("cash_div_tax")), "record_date": parse_date(row.get("record_date")), "ex_date": parse_date(row.get("ex_date")), "pay_date": parse_date(row.get("pay_date")), "data": {key: _coerce(value) for key, value in row.items()}}


def normalize_classification(row: dict[str, object], *, source: str | None = None) -> dict[str, object]:
    returned_source = row.get("src")
    returned_source = returned_source.strip() if isinstance(returned_source, str) else None
    if source and returned_source and returned_source != source:
        raise ValueError("classification source mismatch")
    classification_id = row.get("index_code") or row.get("ts_code") or row.get("industry_code")
    if classification_id in (None, ""):
        raise ValueError("classification id is required")
    name = row.get("industry_name") or row.get("name") or row.get("industry")
    if name in (None, ""):
        raise ValueError("classification name is required")
    raw_level = row.get("level")
    if raw_level in (None, ""):
        level = None
    else:
        level_text = str(raw_level).strip().upper()
        if level_text.startswith("L"):
            level_text = level_text[1:]
        level = int(level_text)
    return {
        "classification_id": str(classification_id),
        "source": source or returned_source or "legacy_unknown",
        "name": str(name),
        "level": level,
        "parent_id": row.get("parent_code") or row.get("parent_id"),
        "data": {key: _coerce(value) for key, value in row.items()},
    }


def normalize_event_row(row: dict[str, object]) -> dict[str, object]:
    return {key: (parse_date(value) if key.endswith("date") or key in {"ann_date", "end_date", "float_date"} else decimal_value(value) if key in {"holder_num", "hold_amount", "revenue"} else value) for key, value in row.items() if key not in {"ts_code"}} | {"ts_code": str(row["ts_code"]), "data": {key: _coerce(value) for key, value in row.items()}}
