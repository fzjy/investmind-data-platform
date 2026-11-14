from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import signal
import subprocess
import time
from threading import Event, Thread
from typing import BinaryIO

from app.ops.backup.contracts import BackupError
from app.ops.backup.budget import remaining_timeout


MAX_CAPTURE_BYTES = 1024 * 1024
BASE_ENV_NAMES = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE"})
EXPLICIT_PG_NAMES = frozenset({"PGPASSFILE", "PGSSLMODE", "PGCONNECT_TIMEOUT"})


def runtime_environment() -> dict[str, str]:
    """不复制供应商配置、PG覆盖变量或 Python/动态库注入变量。"""
    return {name: value for name, value in os.environ.items() if name.upper() in BASE_ENV_NAMES}


def _failure(reason: str) -> BackupError:
    return BackupError(reason, 3, "tools")


class _WindowsJob:
    """Job Object 只管理本调用的进程树，关闭句柄时终止全部成员。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("process containment unavailable")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("process containment unavailable")

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("process containment unavailable")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_process(
    argv: Sequence[str], *, env: Mapping[str, str], timeout: float,
    stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
) -> bytes:
    if not argv or timeout <= 0 or any(not isinstance(value, str) or "\0" in value for value in argv):
        raise BackupError("invalid_tool_arguments", 2, "tools")
    timeout = remaining_timeout(timeout)
    deadline = time.monotonic() + timeout
    environment = {name: value for name, value in env.items() if name.upper() in BASE_ENV_NAMES | EXPLICIT_PG_NAMES}
    options: dict[str, object] = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process: subprocess.Popen[bytes] | None = None
    captured = bytearray()
    overflow = Event()
    reader: Thread | None = None
    job: _WindowsJob | None = None
    try:
        if os.name == "nt":
            job = _WindowsJob()
        process = subprocess.Popen(
            list(argv), stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment, shell=False, **options,
        )
        if job is not None:
            job.attach(process)
        if stdout is None:
            def drain() -> None:
                assert process is not None and process.stdout is not None
                while chunk := process.stdout.read(65536):
                    if len(captured) + len(chunk) > MAX_CAPTURE_BYTES:
                        overflow.set()
                        process.kill()
                        return
                    captured.extend(chunk)
            reader = Thread(target=drain, daemon=True)
            reader.start()
        try:
            process.wait(timeout=max(0.001, remaining_timeout(deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            if job is not None:
                job.close()
            _kill(process)
            raise _failure("tool_timeout") from None
        if job is not None:
            job.close()
        if reader is not None:
            reader.join(timeout=max(0.001, min(2, deadline - time.monotonic())))
            if reader.is_alive():
                _kill(process)
                raise _failure("tool_output_timeout")
        if overflow.is_set():
            raise _failure("tool_output_limit")
        if process.returncode != 0:
            raise BackupError("tool_failed", 3, "tools", process_returncode=process.returncode)
        remaining_timeout(deadline - time.monotonic())
        if time.monotonic() >= deadline:
            raise _failure("tool_timeout")
        return bytes(captured)
    except OSError:
        raise _failure("tool_unavailable") from None
    finally:
        if job is not None:
            job.close()
        if process is not None:
            if process.poll() is None:
                _kill(process)
            if reader is not None:
                reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)
