"""绘制目标抽象：把「可被绘制的东西」统一成取画面、报告守护信号、几何换算的接口。

执行器、守护、画布校验只面向 DrawingTarget，不再假设目标一定是一个 Win32 窗口。
现阶段唯一实现是 Win32Target；后续的 scrcpy 投屏目标同样包一层窗口，
纯 ADB 直连目标则按需降级焦点类信号（见 design §4.5）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.device.geometry import ClientArea
from app.device.service import DeviceService

if TYPE_CHECKING:
    from app.device.capture import CaptureResult
    from app.store.models import CanvasProfile


class DrawingTarget(Protocol):
    """执行器视角下的绘制目标。"""

    kind: str
    lost_hint: str

    def alive(self) -> bool: ...

    def capture(self) -> "CaptureResult": ...

    def capture_image(self): ...

    def client_area(self) -> ClientArea: ...

    def foreground_matches(self) -> bool: ...

    def activate(self) -> bool: ...

    def window_usable(self) -> bool: ...


class Win32Target:
    """目标 = PC 上的一个窗口句柄；行为与原 device+probe 组合逐一对应。"""

    kind = "win32"
    lost_hint = "目标窗口已关闭或句柄失效"

    def __init__(self, hwnd: int, device: DeviceService, probe: object) -> None:
        self.hwnd = hwnd
        self._device = device
        self._probe = probe

    def alive(self) -> bool:
        return self._device.alive(self.hwnd)

    def capture(self) -> "CaptureResult":
        return self._device.capture(self.hwnd)

    def capture_image(self):
        # 校准/面板控制只消费 ndarray，不关心截图来源与后备路径。
        return self._device.capture(self.hwnd).image

    def client_area(self) -> ClientArea:
        return self._device.client_area(self.hwnd)

    def foreground_matches(self) -> bool:
        return bool(self._probe.foreground_matches(self.hwnd))  # type: ignore[attr-defined]

    def activate(self) -> bool:
        return bool(self._probe.activate(self.hwnd))  # type: ignore[attr-defined]

    def window_usable(self) -> bool:
        return bool(self._probe.window_usable(self.hwnd))  # type: ignore[attr-defined]


class MirrorTarget(Win32Target):
    """目标 = 手机投屏窗口（scrcpy）。

    截图、注入、几何、守护与 PC 窗口完全同构，因此直接继承 Win32Target，
    只在 kind 与「设备丢失」提示上做区分——投屏窗口关闭意味着手机侧断开。
    """

    kind = "mirror"
    lost_hint = "投屏窗口已关闭，请确认手机连接与 scrcpy 状态"


def resolve_target(
    calibration: "CanvasProfile", device: DeviceService, probe: object
) -> DrawingTarget:
    """按校准记录的 device_kind 分发；未知种类按 PC 处理（历史数据兼容）。"""
    cls = MirrorTarget if calibration.device_kind == "mirror" else Win32Target
    return cls(calibration.hwnd, device, probe)


def find_live_hwnd_by_title(device: DeviceService, title: str) -> int | None:
    """按窗口标题找回唯一存活的同名窗口句柄。

    小程序重启、窗口重建后句柄会变，但同名窗口只有一个时重绑几乎无歧义；
    同名多个（开了好几个小程序）或一个都没有时返回 None，交回调用方报错。
    """
    if not title:
        return None
    matches = [w.hwnd for w in device.list_windows() if w.title == title]
    return matches[0] if len(matches) == 1 else None
