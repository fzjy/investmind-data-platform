"""仅测试使用的 junction；不运行 shell，也不触碰既有目录。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import struct

import pytest


def make_junction(link: Path, target: Path) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    link.mkdir()
    handle = kernel.CreateFileW(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        pytest.skip(f"junction 创建权限不可用: WinError {ctypes.get_last_error()}")
    try:
        substitute = ("\\??\\" + str(target.resolve())).encode("utf-16le")
        printable = str(target.resolve()).encode("utf-16le")
        paths = substitute + b"\x00\x00" + printable + b"\x00\x00"
        data = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0, 0, len(substitute), len(substitute) + 2, len(printable)) + paths
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, data, len(data), None, 0, ctypes.byref(returned), None):
            pytest.skip(f"junction 创建权限不可用: WinError {ctypes.get_last_error()}")
    finally:
        kernel.CloseHandle(handle)
