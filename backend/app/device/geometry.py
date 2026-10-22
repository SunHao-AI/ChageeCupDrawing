"""窗口客户区与屏幕坐标换算。

换算规则固定为三步：截图图像坐标 → 客户区坐标（乘 clientWidth / imageWidth）
→ 屏幕坐标（加客户区原点）。进程启动时声明 DPI 感知，换算中不再二次缩放。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class ClientArea:
    origin_x: int
    origin_y: int
    width: int
    height: int


def make_dpi_aware() -> None:
    """优先使用 Per-Monitor DPI 感知，失败时退回系统级 DPI 感知。"""
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:
            return
    except OSError:
        pass
    try:
        _user32.SetProcessDPIAware()
    except OSError:
        pass


def get_client_area(hwnd: int) -> ClientArea:
    handle = ctypes.c_void_p(int(hwnd))
    rect = _RECT()
    _user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.restype = wintypes.BOOL
    if not _user32.GetClientRect(handle, ctypes.byref(rect)):
        raise OSError(f"GetClientRect 失败：hwnd={hwnd}")

    origin = _POINT(0, 0)
    _user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT)]
    _user32.ClientToScreen.restype = wintypes.BOOL
    if not _user32.ClientToScreen(handle, ctypes.byref(origin)):
        raise OSError(f"ClientToScreen 失败：hwnd={hwnd}")

    return ClientArea(origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)


def window_dpi_scale(hwnd: int) -> float:
    try:
        _user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        _user32.GetDpiForWindow.restype = wintypes.UINT
        dpi = int(_user32.GetDpiForWindow(ctypes.c_void_p(int(hwnd))))
    except (AttributeError, OSError):
        return 1.0
    if dpi <= 0:
        return 1.0
    return round(dpi / 96.0, 4)


def image_rect_to_client(
    rect: Rect, image_w: int, image_h: int, client: ClientArea
) -> Rect:
    """截图图像坐标换算到客户区坐标。"""
    if image_w <= 0 or image_h <= 0:
        raise ValueError("截图尺寸必须为正数")
    sx = client.width / image_w
    sy = client.height / image_h
    return Rect(
        x=int(round(rect.x * sx)),
        y=int(round(rect.y * sy)),
        w=int(round(rect.w * sx)),
        h=int(round(rect.h * sy)),
    )


def image_rect_to_screen(
    rect: Rect, image_w: int, image_h: int, client: ClientArea
) -> Rect:
    """截图图像坐标直接换算到屏幕坐标。"""
    local = image_rect_to_client(rect, image_w, image_h, client)
    return Rect(
        x=local.x + client.origin_x,
        y=local.y + client.origin_y,
        w=local.w,
        h=local.h,
    )
