from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from pathlib import Path


def create_junction(link: Path, target: Path) -> None:
    """Создаёт Windows junction через Win32 API без запуска командной оболочки."""
    if os.name != "nt":
        raise OSError("Windows junction поддерживается только в Windows.")

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    link.mkdir()
    handle = kernel.CreateFileW(
        str(link),
        0x40000000,
        0,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        link.rmdir()
        raise ctypes.WinError(ctypes.get_last_error())

    substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
    print_name = str(target.resolve()).encode("utf-16-le")
    path_buffer = substitute + b"\0\0" + print_name + b"\0\0"
    data_length = 8 + len(path_buffer)
    reparse_buffer = (
        struct.pack(
            "<LHHHHHH",
            0xA0000003,
            data_length,
            0,
            0,
            len(substitute),
            len(substitute) + 2,
            len(print_name),
        )
        + path_buffer
    )
    input_buffer = ctypes.create_string_buffer(reparse_buffer)
    returned = wintypes.DWORD()
    try:
        if not kernel.DeviceIoControl(
            handle,
            0x000900A4,
            input_buffer,
            len(reparse_buffer),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            link.rmdir()
            raise error
    finally:
        kernel.CloseHandle(handle)
