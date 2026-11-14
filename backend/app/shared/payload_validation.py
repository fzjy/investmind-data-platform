from __future__ import annotations
from collections.abc import Mapping
from math import isfinite


def _is_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or isfinite(value)))



def _matches(value: object, expected: object) -> bool:
    if expected is _is_number:
        return _is_number(value)
    if isinstance(expected, tuple):
        return any(_matches(value, item) for item in expected)
    if isinstance(expected, type):
        return isinstance(value, expected)
    return False



def _at(value: object, *parts: str) -> object:
    current = value
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current



def _validate_record_list(
    value: object,
    path: str,
    fields: Mapping[str, object],
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        errors.append(path)
        return
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{path}[{index}]")
            continue
        for field, expected in fields.items():
            if field not in item or not _matches(item[field], expected):
                errors.append(f"{path}[{index}].{field}")
