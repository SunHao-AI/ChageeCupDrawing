"""I7 设备与投屏目标：统一枚举 PC 窗口 / 安卓设备 / 当前镜像目标。

scrcpy 与 adb 未安装时相关接口明确报 E_SCRCPY_MISSING；「登记已有窗口」用于
接入外部启动的投屏窗口，也用于无手机环境下验证目标注册→校准→绘制→守护链路。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.errors import ApiError
from app.api.schemas import MirrorRegisterRequest, MirrorStartRequest
from app.device.mirror import MirrorError

router = APIRouter()


def _mirror(request: Request):
    service = getattr(request.app.state, "mirror", None)
    if service is None:
        raise ApiError(501, "E_MIRROR_UNSUPPORTED", "当前服务未启用投屏目标")
    return service


def _fail(exc: MirrorError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.detail)


@router.get("/devices")
def list_devices(request: Request) -> dict[str, object]:
    """设备选择器的单一数据源：PC 窗口 + 已授权安卓设备 + 当前镜像状态。"""
    device = request.app.state.device
    mirror = _mirror(request)
    available, missing = mirror.tools_available()
    return {
        "pcWindows": [
            {"hwnd": w.hwnd, "title": w.title, "className": w.class_name}
            for w in device.list_windows()
        ],
        "android": mirror.devices(),
        "mirror": mirror.status().as_dict(),
        "tools": {"available": available, "missing": missing},
    }


@router.post("/devices/mirror")
def start_mirror(payload: MirrorStartRequest, request: Request) -> dict[str, object]:
    """启动 scrcpy 投屏并登记其窗口为绘制目标。"""
    try:
        status = _mirror(request).launch(payload.serial)
    except MirrorError as exc:
        raise _fail(exc) from exc
    return status.as_dict()


@router.post("/devices/mirror/register")
def register_mirror(payload: MirrorRegisterRequest, request: Request) -> dict[str, object]:
    """把已存在的窗口登记为镜像目标（外部启动的 scrcpy，或无手机时验证链路）。"""
    try:
        status = _mirror(request).register_window(
            hwnd=payload.hwnd, serial=payload.serial, label=payload.label
        )
    except MirrorError as exc:
        raise _fail(exc) from exc
    return status.as_dict()


@router.delete("/devices/mirror")
def stop_mirror(request: Request) -> dict[str, object]:
    """结束后端启动的 scrcpy 并清除登记（不动用户手工打开的窗口）。"""
    mirror = _mirror(request)
    mirror.stop_process()
    mirror.clear()
    return mirror.status().as_dict()
