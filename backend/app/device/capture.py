"""截图源：Maa Win32 控制器优先，PrintWindow 降级。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class CaptureError(RuntimeError):
    """截图失败或图像为空。"""


@dataclass(frozen=True)
class CaptureResult:
    image: np.ndarray
    method: str


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MaaCapture:
    """通过 Maa Win32Controller 取窗口图像，返回 RGB 数组。

    控制器按 hwnd 缓存复用：post_connection 内含一次各截图方式的测速
    （失败路径如 DXGI 桌面复制在个别显示器/驱动下耗时且刷错误日志），
    复用连接可把每次截图的固定开销从约 1s 压到约 30ms。
    """

    name = "maa"

    def __init__(self) -> None:
        self._controller = None
        self._controller_hwnd: int | None = None

    def capture(self, hwnd: int) -> CaptureResult:
        try:
            from maa.controller import Win32Controller
            from maa.define import MaaWin32ScreencapMethodEnum
        except ImportError as exc:
            raise CaptureError("MaaFramework 不可用") from exc

        hwnd_int = int(hwnd)
        if self._controller is None or self._controller_hwnd != hwnd_int:
            try:
                controller = Win32Controller(
                    ctypes.c_void_p(hwnd_int),
                    screencap_method=MaaWin32ScreencapMethodEnum.Foreground,
                )
            except RuntimeError as exc:
                raise CaptureError(f"创建 Win32 控制器失败：{exc}") from exc

            if not controller.post_connection().wait().succeeded:
                raise CaptureError("连接目标窗口失败")
            self._controller = controller
            self._controller_hwnd = hwnd_int

        job = self._controller.post_screencap()
        image = job.get(wait=True)
        if image is None or getattr(image, "size", 0) == 0:
            # 窗口可能重建/句柄失效：丢弃缓存，下次重建连接。
            self._controller = None
            self._controller_hwnd = None
            raise CaptureError("截图返回空图像")

        # Maa 返回 BGR 三通道，统一转成 RGB。
        rgb = np.ascontiguousarray(np.asarray(image)[:, :, 2::-1])
        return CaptureResult(image=rgb, method=self.name)


class PrintWindowCapture:
    """ctypes 调用 PrintWindow 的降级实现。"""

    name = "win32"

    def capture(self, hwnd: int) -> CaptureResult:
        image = _printwindow(hwnd)
        return CaptureResult(image=image, method=self.name)


def _printwindow(hwnd: int) -> np.ndarray:
    handle = ctypes.c_void_p(int(hwnd))

    _user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.restype = wintypes.BOOL
    rect = _RECT()
    if not _user32.GetClientRect(handle, ctypes.byref(rect)):
        raise CaptureError("读取窗口客户区失败")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise CaptureError("窗口客户区尺寸为 0")

    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.GetDC.restype = ctypes.c_void_p
    hdc_window = _user32.GetDC(handle)
    if not hdc_window:
        raise CaptureError("获取窗口 DC 失败")

    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    hdc = _gdi32.CreateCompatibleDC(ctypes.c_void_p(hdc_window))

    hbitmap = 0
    old = 0
    bits = ctypes.c_void_p()
    try:
        if not hdc:
            raise CaptureError("创建兼容 DC 失败")

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 负高度表示自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        _gdi32.CreateDIBSection.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        _gdi32.CreateDIBSection.restype = ctypes.c_void_p
        hbitmap = _gdi32.CreateDIBSection(
            ctypes.c_void_p(hdc), ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
        if not hbitmap or not bits.value:
            raise CaptureError("创建 DIB 失败")

        _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _gdi32.SelectObject.restype = ctypes.c_void_p
        old = _gdi32.SelectObject(ctypes.c_void_p(hdc), ctypes.c_void_p(hbitmap))

        _user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        _user32.PrintWindow.restype = wintypes.BOOL
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        if not _user32.PrintWindow(handle, ctypes.c_void_p(hdc), 3):
            raise CaptureError("PrintWindow 截图失败")

        raw = ctypes.string_at(bits.value, width * height * 4)
    finally:
        if old and hdc:
            _gdi32.SelectObject(ctypes.c_void_p(hdc), ctypes.c_void_p(old))
        if hbitmap:
            _gdi32.DeleteObject(ctypes.c_void_p(hbitmap))
        if hdc:
            _gdi32.DeleteDC(ctypes.c_void_p(hdc))
        _user32.ReleaseDC(handle, ctypes.c_void_p(hdc_window))

    bgra = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
    return np.ascontiguousarray(bgra[:, :, 2::-1])


class AutoCapture:
    """按首选顺序尝试截图实现，全部失败时抛出最后一个错误。"""

    def __init__(self) -> None:
        self._maa = MaaCapture()
        self._win32 = PrintWindowCapture()

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        sources = [self._maa, self._win32]
        if prefer == "win32":
            sources.reverse()

        errors: list[str] = []
        for source in sources:
            try:
                return source.capture(hwnd)
            except CaptureError as exc:
                errors.append(f"{source.name}: {exc}")
        raise CaptureError("；".join(errors) or "截图失败")
