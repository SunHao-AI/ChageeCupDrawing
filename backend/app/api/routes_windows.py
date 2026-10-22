"""I2 桌面窗口枚举。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/windows")
def list_windows(request: Request) -> list[dict[str, object]]:
    device = request.app.state.device
    return [
        {"hwnd": window.hwnd, "title": window.title, "className": window.class_name}
        for window in device.list_windows()
    ]
