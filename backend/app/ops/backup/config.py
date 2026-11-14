from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import SecretStr

from app.config import PROJECT_ROOT, Settings
from app.ops.backup.contracts import BackupError, BackupOptions, ConnectionTarget


def backup_options(settings: Settings) -> BackupOptions:
    path = Path(settings.backup_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    # 不 resolve：文件安全层需要先检查未解析路径中的链接/junction。
    return BackupOptions(
        tool_mode=settings.backup_tool_mode, path=path,
        native_bin=Path(settings.backup_native_bin) if settings.backup_native_bin else None,
        docker_image=settings.backup_docker_image,
        interval_days=settings.backup_interval_days, keep_count=settings.backup_keep_count,
        retention_days=settings.backup_retention_days, timeout_seconds=settings.backup_timeout_seconds,
    )


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def parse_target(database_url: str) -> ConnectionTarget:
    try:
        if not isinstance(database_url, str) or _has_control(database_url):
            raise ValueError
        url = urlsplit(database_url)
        if url.scheme not in {"postgresql", "postgresql+psycopg"} or url.fragment:
            raise ValueError
        host = url.hostname
        username = unquote(url.username or "")
        password = unquote(url.password or "")
        database = unquote(url.path.removeprefix("/"))
        if not host or not username or not database:
            raise ValueError
        if any(_has_control(value) for value in (host, username, password, database)):
            raise ValueError
        if any(char in host for char in ",/%\\") or any(char.isspace() for char in host) or any(char in database for char in "/=") or "," in url.netloc.rsplit("@", 1)[-1]:
            raise ValueError
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            host = host.lower().rstrip(".")
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(pairs) != len(query) or set(query) - {"sslmode", "connect_timeout"}:
            raise ValueError
        timeout = query.get("connect_timeout", "10")
        if not timeout.isascii() or not timeout.isdigit():
            raise ValueError
        return ConnectionTarget(
            host=host, port=url.port if url.port is not None else 5432,
            database=database, username=username, password=SecretStr(password),
            sslmode=query.get("sslmode", "prefer"), connect_timeout=int(timeout),
        )
    except (ValueError, TypeError, OverflowError):
        raise BackupError("invalid_database_configuration", 2, "configuration") from None


def source_identity(target: ConnectionTarget) -> str:
    encoded = json.dumps([target.host, target.port, target.database], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
