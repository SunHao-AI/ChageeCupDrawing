"""桌面窗口枚举与存活性查询。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str


def _hwnd_value(raw: object) -> int:
    value = getattr(raw, "value", raw)
    return int(value or 0)


def list_windows() -> list[WindowInfo]:
    from maa.toolkit import Toolkit

    windows: list[WindowInfo] = []
    for item in Toolkit.find_desktop_windows():
        hwnd = _hwnd_value(item.hwnd)
        if not hwnd:
            continue
        windows.append(
            WindowInfo(hwnd=hwnd, title=str(item.window_name), class_name=str(item.class_name))
        )
    return windows


def window_alive(hwnd: int) -> bool:
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = wintypes.BOOL
    return bool(_user32.IsWindow(ctypes.c_void_p(int(hwnd))))


def foreground_hwnd() -> int:
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    return _hwnd_value(_user32.GetForegroundWindow())


def window_title(hwnd: int) -> str:
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(ctypes.c_void_p(int(hwnd)), buffer, len(buffer))
    return buffer.value
