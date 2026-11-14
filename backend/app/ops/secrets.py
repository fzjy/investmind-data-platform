"""挂载密钥的受限读取及已解析配置的日志脱敏输入。"""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from app.config import Settings


SECRET_FIELDS = ("database_url", "database_read_url", "tushare_token", "fred_api_key")
MAX_SECRET_FILE_BYTES = 64 * 1024
INVALID_SECRET_FILE = "invalid mounted secret configuration"


def read_mounted_secret(file_path: str | None) -> str:
    """仅接受绝对路径的 UTF-8 普通文件，保留密钥本身的空格。"""
    try:
        if not file_path or any(ord(char) < 32 or ord(char) == 127 for char in file_path):
            raise ValueError
        path = Path(file_path)
        if not path.is_absolute():
            raise ValueError
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SECRET_FILE_BYTES:
            raise ValueError
        # 跟随挂载服务的轮换符号链接，但打开后仍检查文件类型与大小。
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_SECRET_FILE_BYTES:
                raise ValueError
            payload = stream.read(MAX_SECRET_FILE_BYTES + 1)
        if len(payload) > MAX_SECRET_FILE_BYTES:
            raise ValueError
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]
        value = payload.decode("utf-8")
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        return value
    except (OSError, ValueError):
        # OS/解码异常可能包含文件路径、字节和值，禁止向外传播原始异常文本。
        raise ValueError(INVALID_SECRET_FILE) from None


def file_secret_overrides(
    dotenv: Mapping[str, str | None],
    fields: tuple[str, ...] = SECRET_FIELDS,
) -> dict[str, str]:
    """进程 _FILE > dotenv _FILE；存在即必须成功，明文不能覆盖或兜底。"""
    process_values = {name.casefold(): value for name, value in os.environ.items()}
    dotenv_values = {name.casefold(): value for name, value in dotenv.items()}
    overrides: dict[str, str] = {}
    for field in fields:
        name = "dang_" + field + "_file"
        if name in process_values:
            overrides[field] = read_mounted_secret(process_values[name])
        elif name in dotenv_values:
            overrides[field] = read_mounted_secret(dotenv_values[name])
    return overrides


def resolved_secret_values(settings: Settings) -> tuple[str, ...]:
    """供脱敏器登记实际生效的值；返回值本身不可记录到日志。"""
    values: list[str] = []
    for url in (settings.database_url, settings.database_read_url):
        if url:
            values.append(url)
            try:
                password = urlsplit(url).password
            except ValueError:
                password = None
            if password:
                values.extend((password, unquote(password)))
    for secret in (settings.tushare_token, settings.fred_api_key):
        if secret and secret.get_secret_value():
            values.append(secret.get_secret_value())
    return tuple(dict.fromkeys(values))
