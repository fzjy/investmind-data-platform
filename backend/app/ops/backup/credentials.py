from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator

from app.ops.backup.contracts import BackupError, ConnectionTarget


def windows_user_sid() -> str:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("token unavailable")
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise OSError("token unavailable")
        sid = ctypes.cast(data, ctypes.POINTER(wintypes.LPVOID)).contents.value
        result = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise OSError("sid unavailable")
        try:
            return result.value
        finally:
            kernel.LocalFree(result)
    finally:
        kernel.CloseHandle(token)


def windows_private_acl(path: Path) -> bool:
    """检查实际 ACE，只接受当前用户的允许条目。"""
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID)]
    security.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID()
    if security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) != 0:
        return False
    try:
        if not dacl.value:
            return False
        # ACL: revision BYTE, sbz BYTE, size WORD, count WORD, sbz WORD。
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        sid_expected = windows_user_sid()
        found = False
        for index in range(count):
            ace = wintypes.LPVOID()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            if ctypes.c_ubyte.from_address(ace.value).value != 0:
                return False
            result = wintypes.LPWSTR()
            if not security.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(result)):
                return False
            try:
                if result.value != sid_expected:
                    return False
                found = True
            finally:
                kernel.LocalFree(result)
        return found
    finally:
        kernel.LocalFree(descriptor)


def restrict_directory(path: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [wintypes.LPVOID]
        security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
        security.GetSecurityDescriptorDacl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL)]
        security.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID]
        descriptor, dacl = wintypes.LPVOID(), wintypes.LPVOID()
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        # 替换整个 DACL：只关闭继承无法移除 Python 创建目录时的显式 ACE。
        try:
            sddl = "D:P(A;OICI;FA;;;" + windows_user_sid() + ")"
            if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                raise OSError("private acl unavailable")
            if not security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
                raise OSError("private acl unavailable")
            if security.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None) != 0:
                raise OSError("private acl unavailable")
        finally:
            if descriptor:
                kernel.LocalFree(descriptor)
        if not windows_private_acl(path):
            raise OSError("private acl unavailable")
    else:
        path.chmod(0o700)
        if path.stat().st_mode & 0o077:
            raise OSError("private directory unavailable")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def temporary_passfile(target: ConnectionTarget) -> Iterator[Path]:
    directory: Path | None = None
    path: Path | None = None
    try:
        directory = Path(tempfile.mkdtemp(prefix="smartparse-pgpass-"))
        try:
            restrict_directory(directory)
            path = directory / "pgpass"
            fields = (target.host, str(target.port), target.database, target.username, target.password.get_secret_value())
            if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in fields):
                raise OSError("invalid passfile field")
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((":".join(_escape(value) for value in fields) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt":
                if not windows_private_acl(path):
                    raise OSError("private file unavailable")
            elif path.stat().st_mode & 0o077:
                raise OSError("private file unavailable")
        except (OSError, BackupError):
            raise BackupError("passfile_permissions_unavailable", 2, "credentials") from None
        yield path
    finally:
        if path is not None and path.exists():
            path.unlink()
        if directory is not None:
            directory.rmdir()
