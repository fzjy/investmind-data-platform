"""跨 SQL、工具准备、文件写入共享期限；安全回收使用独立有限预算。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time
from typing import Iterator

from app.ops.backup.contracts import BackupError


_deadline: ContextVar[float | None] = ContextVar("backup_deadline", default=None)


def check_budget() -> None:
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise BackupError("backup_budget_exhausted", 3, "timeout")


def remaining_timeout(requested: float) -> float:
    deadline = _deadline.get()
    value = requested if deadline is None else min(requested, deadline - time.monotonic())
    if value <= 0:
        raise BackupError("backup_budget_exhausted", 3, "timeout")
    return value


@contextmanager
def deadline_scope(timeout: float) -> Iterator[None]:
    previous = _deadline.get()
    deadline = time.monotonic() + timeout
    token = _deadline.set(deadline if previous is None else min(previous, deadline))
    try:
        check_budget()
        yield
        check_budget()
    finally:
        _deadline.reset(token)


@contextmanager
def cleanup_budget() -> Iterator[None]:
    # 主流程已经超时也必须尝试回收确认属于自己的资源，不延长业务工作预算。
    token = _deadline.set(None)
    try:
        with deadline_scope(30):
            yield
    finally:
        _deadline.reset(token)


def settings_budget(function):
    @wraps(function)
    def call(settings, *args, **kwargs):
        with deadline_scope(settings.backup_timeout_seconds):
            return function(settings, *args, **kwargs)
    return call
