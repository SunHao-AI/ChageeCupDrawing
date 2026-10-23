"""设备服务：为 API 层提供窗口枚举、截图与几何信息，可被测试替身替换。"""

from __future__ import annotations

from app.device.capture import AutoCapture, CaptureError, CaptureResult
from app.device.geometry import (
    ClientArea,
    get_client_area,
    window_dpi_scale,
)
from app.device.windows import WindowInfo, foreground_hwnd, list_windows, window_alive, window_title

__all__ = [
    "CaptureError",
    "CaptureResult",
    "DeviceService",
    "WindowInfo",
]


class DeviceService:
    def __init__(self, capture: AutoCapture | None = None) -> None:
        self._capture = capture or AutoCapture()

    def list_windows(self) -> list[WindowInfo]:
        return list_windows()

    def alive(self, hwnd: int) -> bool:
        return window_alive(hwnd)

    def foreground(self) -> int:
        return foreground_hwnd()

    def title(self, hwnd: int) -> str:
        return window_title(hwnd)

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        return self._capture.capture(hwnd, prefer)

    def client_area(self, hwnd: int) -> ClientArea:
        return get_client_area(hwnd)

    def dpi_scale(self, hwnd: int) -> float:
        return window_dpi_scale(hwnd)
