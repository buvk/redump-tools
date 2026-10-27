"""Lightweight file identity with a real metadata-change timestamp on Windows."""
from __future__ import annotations

import ctypes
import os
import sys
from functools import cache
from pathlib import Path


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [("CreationTime", ctypes.c_int64), ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64), ("ChangeTime", ctypes.c_int64), ("FileAttributes", ctypes.c_uint32)]


if sys.platform == "win32":
    @cache
    def _windows_api():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        query.restype = ctypes.c_int
        close = kernel.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        return create, query, close


    def _windows_change_time(path: Path) -> int:
        create, query, close = _windows_api()
        name = str(path.resolve())
        if not name.startswith("\\\\?\\"):
            name = "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
        # Read attributes only; permit other applications to read, write, and rename.
        handle = create(name, 0x80, 7, None, 3, 0x02000000, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileBasicInfo()
            if not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.ChangeTime <= 0:
                raise OSError(f"File change time unavailable: {path}")
            return info.ChangeTime * 100
        finally:
            close(handle)
else:
    def _windows_change_time(path: Path) -> int:
        raise OSError(f"Windows change-time queries are unavailable on this platform: {path}")


def file_fingerprint(path: Path) -> list[int]:
    before = path.stat()
    change = _windows_change_time(path) if os.name == "nt" else before.st_ctime_ns
    after = path.stat()
    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino
    if identity(before) != identity(after):
        raise OSError(f"File changed while reading its identity: {path}")
    return [before.st_size, before.st_mtime_ns, change, before.st_dev, before.st_ino]
