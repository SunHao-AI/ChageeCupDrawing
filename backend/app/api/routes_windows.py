"""I2 桌面窗口枚举 + 校准参考截图。"""

from __future__ import annotations

import io

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import Response
from PIL import Image

from app.api.errors import ApiError
from app.device.capture import CaptureError

router = APIRouter()


@router.get("/windows")
def list_windows(request: Request) -> list[dict[str, object]]:
    device = request.app.state.device
    return [
        {"hwnd": window.hwnd, "title": window.title, "className": window.class_name}
        for window in device.list_windows()
    ]


@router.get("/windows/{hwnd}/capture")
def window_capture(hwnd: int, request: Request) -> Response:
    """手动校准的参考截图：实时抓取窗口客户区并编码为 PNG。"""
    device = request.app.state.device
    if not device.alive(hwnd):
        raise ApiError(409, "E_WINDOW_LOST", "目标窗口已关闭")
    try:
        result = device.capture(hwnd)
    except CaptureError as exc:
        raise ApiError(422, "E_CAPTURE_FAILED", "窗口截图失败", str(exc)) from exc

    image = Image.fromarray(np.asarray(result.image, dtype=np.uint8))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Capture-Method": result.method},
    )
