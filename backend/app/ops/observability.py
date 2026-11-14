"""CLI 结构化事件与本地告警；不截获 stdout、不执行网络请求。

接线：在全部 command/add_typer 注册后、app() 调用前执行
install_cli_observability(app)。将需要报告关联的 JSON echo 改为
emit_report(payload, err=False)；错误报告可以传 err=True。
原有 echo 无需改动即可记录命令结果，但不会推测其报告字段。
默认日志写 stderr，外部收集器可用标准 logging handler 消费
LogRecord.structured_event / LogRecord.alert，不需要新增配置。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
import inspect
import json
import logging
import re
import sys
from time import perf_counter
from typing import ParamSpec, TypeVar, get_type_hints
from urllib.parse import unquote, urlsplit

import click
from pydantic import SecretStr
import typer

from app.config import Settings
from app.connectors.macro.sanitization import sanitize_macro_text, sanitize_macro_value


LOGGER_NAME = "app.ops.observability"
DEFAULT_SAFETY_CRITICAL_COMMANDS = frozenset({
    "build-and-publish", "daily-sync", "macro-sync", "rebuild-marts",
    "backup-database", "verify-database-backup",
})
_SECRET_FIELD = re.compile(r"(?i)(token|password|passwd|secret|api_?key|database.*url)")
_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass
class _Invocation:
    command: str
    secrets: tuple[str, ...] = ()
    fields: dict[str, object] = field(default_factory=dict)
    settings_error_type: str | None = None


_CURRENT: ContextVar[_Invocation | None] = ContextVar("cli_observability", default=None)


class _StderrHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # 每次写入时读取当前 stderr，避免重复 CliRunner 调用留下已关闭流。
        sys.stderr.write(record.getMessage() + "\n")
        sys.stderr.flush()


def _logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(handler, _StderrHandler) for handler in logger.handlers):
        logger.addHandler(_StderrHandler())
        logger.setLevel(logging.INFO)
    return logger


def _settings_secrets(settings: Settings) -> tuple[str, ...]:
    # _FILE 的解析属于 Settings；从解析后的对象读取，绝不记录或另读 Secret 文件。
    secrets: set[str] = set()
    for name, value in vars(settings).items():
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if secret:
                secrets.add(secret)
        elif isinstance(value, str) and _SECRET_FIELD.search(name):
            if value:
                secrets.add(value)
                try:
                    password = urlsplit(value).password
                except ValueError:
                    password = None
                if password:
                    secrets.update((password, unquote(password)))
    return tuple(sorted(secrets, key=len, reverse=True))


def _write_event(event: Mapping[str, object], level: int, secrets: tuple[str, ...] = ()) -> None:
    safe = sanitize_macro_value(dict(event), *secrets)
    if not isinstance(safe, dict):
        return
    extra: dict[str, object] = {"structured_event": safe}
    if "alert" in safe:
        extra["alert"] = safe["alert"]
    try:
        _logger().log(level, json.dumps(safe, ensure_ascii=False, allow_nan=False), extra=extra)
    except Exception:
        # 外接收集器不可用时保留原业务结果/异常；默认本地 handler 仍先写 stderr。
        # 不回显 handler 异常，避免它夹带请求地址、认证头或凭据。
        pass


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _report_fields(payload: Mapping[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key in ("jobRunId", "version", "releaseVersion", "publicationVersion"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    if isinstance(payload.get("status"), str):
        fields["state"] = payload["status"]
    for key in ("scale", "budgets"):
        if isinstance(payload.get(key), Mapping):
            fields[key] = dict(payload[key])

    # 根总数优先；否则选择一个完整明细层，不能同时累加 plans 与 batches。
    batches = payload.get("batches")
    plans = payload.get("plans")
    details: list[Mapping[str, object]] = []
    for candidate in (batches, plans, payload.get("outcomes")):
        if isinstance(candidate, list) and candidate and all(isinstance(item, Mapping) for item in candidate):
            details = candidate
            break
    for key, aliases in (("rowCount", ("rowCount", "rows")),
                         ("rejectedCount", ("rejectedCount", "rejected"))):
        for alias in aliases:
            if _count(payload.get(alias)):
                fields[key] = payload[alias]
                break
        else:
            if details and all(_count(item.get(key)) for item in details):
                fields[key] = sum(item[key] for item in details)

    batch_ids: list[str] = []
    for source in (payload, *details):
        single = source.get("batchId")
        if isinstance(single, str) and single:
            batch_ids.append(single)
        multiple = source.get("batchIds")
        if isinstance(multiple, Sequence) and not isinstance(multiple, str | bytes):
            batch_ids.extend(item for item in multiple if isinstance(item, str) and item)
    if batch_ids:
        fields["batchIds"] = list(dict.fromkeys(batch_ids))
    return fields


def emit_report(payload: Mapping[str, object], *, err: bool = False) -> None:
    """一次输出一个 JSON 报告，同时关联当前命令；不缓存 stdout 或原始报告。"""
    current = _CURRENT.get()
    secrets = current.secrets if current is not None else ()
    safe = sanitize_macro_value(dict(payload), *secrets)
    encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    typer.echo(encoded, err=err)
    if current is not None and isinstance(safe, Mapping):
        # 最后一个完整报告胜出，避免多次 emit 导致混合不同 job/版本。
        current.fields = _report_fields(safe)


def emit_safety_alert(code: str, message: str, **fields: object) -> None:
    """向本地/外接 logging handler 发 CRITICAL 告警，不包含任何真实发送逻辑。"""
    current = _CURRENT.get()
    event: dict[str, object] = {
        **fields,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "level": "critical", "event": "safety_alert",
        "alert": {"code": code, "message": message},
    }
    if current is not None:
        event.update(current.fields)
        event["command"] = current.command
    _write_event(event, logging.CRITICAL, current.secrets if current is not None else ())


def _observe(
    callback: Callable[_P, _R], command: str, settings_loader: Callable[[], Settings],
    safety_critical_commands: frozenset[str],
) -> Callable[_P, _R]:
    @wraps(callback)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        started = perf_counter()
        current = _Invocation(command)
        try:
            current.secrets = _settings_secrets(settings_loader())
        except Exception as error:
            # 未能解析凭据时，配置异常仅记录类型，不能回显可能含明文的输入。
            current.settings_error_type = type(error).__name__
        token = _CURRENT.set(current)
        exit_code = 0
        error_type: str | None = None
        error_message: str | None = None
        try:
            return callback(*args, **kwargs)
        except BaseException as error:
            if isinstance(error, typer.Exit):
                exit_code = error.exit_code
            elif isinstance(error, SystemExit):
                exit_code = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
            elif isinstance(error, (click.ClickException, typer.BadParameter)):
                exit_code = error.exit_code
            else:
                exit_code = 1
            if exit_code:
                error_type = type(error).__name__
                if not current.settings_error_type:
                    error_message = sanitize_macro_text(str(error), *current.secrets)
            raise
        finally:
            event: dict[str, object] = {
                **current.fields,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "level": "info" if exit_code == 0 else "error", "event": "cli_command",
                "command": command,
                "state": current.fields.get("state", "succeeded" if exit_code == 0 else "failed"),
                "outcome": "succeeded" if exit_code == 0 else "failed",
                "exitCode": exit_code,
                "durationMs": round((perf_counter() - started) * 1000, 3),
            }
            if error_type:
                event["errorType"] = error_type
            if error_message:
                event["message"] = error_message
            if current.settings_error_type:
                event["settingsErrorType"] = current.settings_error_type
            try:
                _write_event(event, logging.INFO if exit_code == 0 else logging.ERROR, current.secrets)
                if exit_code and (command in safety_critical_commands or command.split()[-1] in safety_critical_commands):
                    emit_safety_alert("SAFETY_CRITICAL_COMMAND_FAILED", "安全关键命令执行失败",
                                      exitCode=exit_code, outcome="failed")
            finally:
                _CURRENT.reset(token)

    # Typer 会求值注解；包装函数 globals 与原函数不同，必须提前解析原注解。
    wrapped.__annotations__ = get_type_hints(callback, include_extras=True)
    setattr(wrapped, "__signature__", inspect.signature(callback, eval_str=True))
    setattr(wrapped, "_cli_observability_installed", True)
    return wrapped


def install_cli_observability(
    app: typer.Typer, settings_loader: Callable[[], Settings] = Settings.from_env, *,
    safety_critical_commands: frozenset[str] = DEFAULT_SAFETY_CRITICAL_COMMANDS,
) -> None:
    """幂等包装已注册命令及子组；必须在注册完成后调用，参数解析仍由 Typer 负责。

    不包装 --help 或尚未进入命令回调的参数解析错误。报告关联仅通过
    emit_report；安全关键列表可由主入口按业务范围显式覆盖。
    """
    def install(target: typer.Typer, prefix: tuple[str, ...] = ()) -> None:
        for info in target.registered_commands:
            callback = info.callback
            if callback is None or getattr(callback, "_cli_observability_installed", False):
                continue
            name = info.name or callback.__name__.replace("_", "-")
            info.callback = _observe(callback, " ".join((*prefix, name)), settings_loader, safety_critical_commands)
        for group in target.registered_groups:
            if group.typer_instance is not None:
                name = group.name or group.typer_instance.info.name
                install(group.typer_instance, (*prefix, name) if isinstance(name, str) else prefix)

    install(app)
