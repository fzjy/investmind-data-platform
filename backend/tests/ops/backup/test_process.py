from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.ops.backup.contracts import BackupError


def module():
    assert importlib.util.find_spec("app.ops.backup.process") is not None, "safe process execution is missing"
    return importlib.import_module("app.ops.backup.process")


def test_binary_roundtrip_with_spaced_unicode_paths(tmp_path: Path) -> None:
    runner = module().run_process
    source = tmp_path / "含 空格 input.bin"
    destination = tmp_path / "含 空格 output.bin"
    source.write_bytes(b"\x00\xff\r\n")
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        result = runner([sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], env=os.environ, timeout=10, stdin=incoming, stdout=outgoing)
    assert result == b""
    assert destination.read_bytes() == b"\x00\xff\r\n"


def test_environment_does_not_inherit_supplier_or_pg_overrides() -> None:
    runner = module().run_process
    environment = dict(os.environ, DANG_TUSHARE_TOKEN="secret-canary", DANG_FRED_API_KEY="secret-canary", PGPASSWORD="secret-canary", PGHOST="business-canary", PYTHONPATH="business-canary")
    script = "import os; print(','.join(sorted(k for k in os.environ if k.upper().startswith(('DANG_', 'PG', 'PYTHON')))))"
    assert runner([sys.executable, "-I", "-c", script], env=environment, timeout=10).strip() == b""


def test_failure_never_exposes_raw_stderr() -> None:
    runner = module().run_process
    with pytest.raises(BackupError) as caught:
        runner([sys.executable, "-I", "-c", "import sys; sys.stderr.write('postgresql://secret-canary'); sys.exit(1)"], env=os.environ, timeout=10)
    assert caught.value.exit_code == 3
    assert "secret-canary" not in str(caught.value)
    assert "postgresql" not in str(caught.value)


def test_child_exit_status_is_available_without_raw_error_text() -> None:
    with pytest.raises(BackupError) as caught:
        module().run_process([sys.executable, "-I", "-c", "import sys; sys.stderr.write('secret-canary'); sys.exit(4)"], env=os.environ, timeout=10)
    assert getattr(caught.value, "process_returncode", None) == 4
    assert caught.value.exit_code == 3
    assert "secret-canary" not in str(caught.value)


def test_timeout_kills_owned_process(tmp_path: Path) -> None:
    runner = module().run_process
    pid_file = tmp_path / "pid.txt"
    script = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(BackupError) as caught:
        runner([sys.executable, "-I", "-c", script, str(pid_file)], env=os.environ, timeout=1)
    assert caught.value.reason_code == "tool_timeout"
    pid = int(pid_file.read_text())
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            assert code.value != 259
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_output_is_bounded() -> None:
    runner = module().run_process
    with pytest.raises(BackupError) as caught:
        runner([sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))"], env=os.environ, timeout=10)
    assert caught.value.reason_code == "tool_output_limit"


def test_timeout_also_stops_descendant_process(tmp_path: Path) -> None:
    runner = module().run_process
    pid_file = tmp_path / "child-pid.txt"
    child_script = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    parent_script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]]); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(BackupError):
        runner([sys.executable, "-I", "-c", parent_script, child_script, str(pid_file)], env=os.environ, timeout=1)
    elapsed = time.monotonic() - started
    pid = int(pid_file.read_text())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000 | 1, False, pid)
        if handle:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            try:
                assert code.value != 259, "owned child survived timeout"
            finally:
                # 失败反例也回收此测试刚创建的进程，不能遗留到其他测试。
                kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
                if code.value == 259:
                    kernel.TerminateProcess(handle, 1)
                kernel.CloseHandle(handle)
        assert elapsed < 5, "timeout waited for surviving descendant to close stdout"
    else:
        # killpg 后子进程可短暂成为 zombie；不能继续执行。
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        state = Path(f"/proc/{pid}/stat")
        if state.exists():
            assert state.read_text().split()[2] == "Z"
        else:
            pytest.fail("owned child survived timeout")


def test_argv_is_not_interpreted_by_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    process = module()
    original = subprocess.Popen
    observed: dict[str, object] = {}
    def record(argv, **kwargs):
        observed.update(kwargs)
        return original(argv, **kwargs)
    monkeypatch.setattr(process.subprocess, "Popen", record)
    output = process.run_process([sys.executable, "-I", "-c", "import sys; print(sys.argv[1])", "literal & echo canary"], env=os.environ, timeout=10)
    assert output.strip() == b"literal & echo canary"
    assert observed.get("shell") is False
    if os.name == "nt":
        assert observed["creationflags"] & subprocess.CREATE_NO_WINDOW
