from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import math
import re


_SECRET_KEY_SUFFIXES = (
    ("api", "key"),
    ("apikey",),
    ("access", "token"),
    ("accesstoken",),
    ("client", "secret"),
    ("clientsecret",),
    ("secret", "key"),
    ("secretkey",),
    ("authorization",),
    ("password",),
    ("token",),
    ("secret",),
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_TOKEN = re.compile(r"[a-z0-9]+")
_SECRET_KEY_NAME = (
    # 单次标识符扫描，避免长全大写机器码触发嵌套前缀的灾难性回溯。
    # 实际命名空间/camelCase密钥边界仍由_is_secret_key判定。
    r"(?=[a-z])[a-z0-9_-]*"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key|authorization|password|token|secret)"
)
_KEY_VALUE = re.compile(
    rf"""(?ix)
    (?<![a-z0-9_-])
    (?P<prefix>
        (?P<quote>["']?)
        (?P<key>{_SECRET_KEY_NAME})
        (?P=quote)
        \s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        (?:bearer\s+)?[^\s,;&#]+
    )
    """
)
_BEARER_VALUE = re.compile(
    r"(?i)\bbearer(?P<separator>\s+)(?P<value>[^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@"
)


def sanitize_macro_text(value: str, *secrets: str) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    redacted = _KEY_VALUE.sub(_redact_secret_assignment, redacted)
    redacted = _BEARER_VALUE.sub(
        lambda match: f"Bearer{match.group('separator')}***",
        redacted,
    )
    return _URL_CREDENTIALS.sub(r"\1***@", redacted)


def sanitize_macro_value(value: object, *secrets: str) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_macro_value(nested, *secrets)
            for key, nested in value.items()
            if not _is_secret_key(str(key))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [sanitize_macro_value(item, *secrets) for item in value]
    if isinstance(value, str):
        return sanitize_macro_text(value, *secrets)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return {
            "omittedBinary": True,
            "contentSha256": sha256(bytes(value)).hexdigest(),
        }
    if value is None or isinstance(value, bool | int | float):
        return value
    return sanitize_macro_text(str(value), *secrets)


def _is_secret_key(key: str) -> bool:
    tokens = tuple(
        _KEY_TOKEN.findall(_CAMEL_CASE_BOUNDARY.sub(" ", key).casefold())
    )
    return any(
        len(tokens) >= len(suffix) and tokens[-len(suffix) :] == suffix
        for suffix in _SECRET_KEY_SUFFIXES
    )


def _redact_secret_assignment(match: re.Match[str]) -> str:
    if not _is_secret_key(match.group("key")):
        return match.group(0)
    value = match.group("value")
    if value[0] in {'"', "'"}:
        replacement = f"{value[0]}***{value[0]}"
    elif value.casefold().startswith("bearer "):
        replacement = f"{value[: len('bearer ')]}***"
    else:
        replacement = "***"
    return f"{match.group('prefix')}{replacement}"
