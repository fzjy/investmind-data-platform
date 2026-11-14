"""稳定血缘指纹；序列化规则不随领域路径变化。"""
from datetime import date, datetime, timezone
from decimal import Decimal
from collections.abc import Mapping
import hashlib
import json

def _canonicalize(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": format(value, "f")}
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo else value
        return {"__type__": "datetime", "value": normalized.isoformat()}
    if isinstance(value, date):
        return {"__type__": "date", "value": value.isoformat()}
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        return {"__type__": "tuple", "items": [_canonicalize(item) for item in value]}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, set):
        items = [_canonicalize(item) for item in value]
        return {
            "__type__": "set",
            "items": sorted(
                items,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        }
    return {"__type__": type(value).__qualname__, "value": str(value)}



def canonical_source_hash(
    value: object,
    source_table: str,
    source_record_key: str,
) -> str:
    payload = {
        "source_record_key": source_record_key,
        "source_table": source_table,
        "value": _canonicalize(value),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
