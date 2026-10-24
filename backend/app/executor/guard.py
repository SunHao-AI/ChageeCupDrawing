"""输入主权守护：绘制期间持续检测目标窗口与鼠标归属，命中即停笔。

守护线程每 guard_poll_ms 轮询一次信号，主绘制循环只在既有采样点消费停止原因，
二者通过 _RunHandle 上的事件通信，主循环不新增任何系统调用。
设计见 docs/focus-guard-and-multidevice-design.md §3。
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.device.target import DrawingTarget

VK_ESCAPE = 0x1B
VK_LBUTTON = 0x01
# 注入器 up 之后该时段内读到的左键按下位来自笔迹本身，不算用户接管。
BUTTON_EXEMPT_S = 0.15

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class GuardProbe(Protocol):
    """守护信号的数据源；测试以假实现替换，避免依赖真实桌面状态。"""

    def clear_escape_state(self) -> None: ...

    def foreground_matches(self, hwnd: int) -> bool: ...

    def activate(self, hwnd: int) -> bool: ...

    def window_usable(self, hwnd: int) -> bool: ...

    def cursor_pos(self) -> tuple[int, int]: ...

    def left_button_pressed(self) -> bool: ...

    def esc_pressed(self) -> bool: ...


class HandleLike(Protocol):
    """_RunHandle 的守护视角：阶段标志 + 静置光标位置。"""

    guard_active: threading.Event
    stroking: threading.Event
    settled_cursor: tuple[int, int] | None


class Win32GuardProbe:
    def clear_escape_state(self) -> None:
        _user32.GetAsyncKeyState(VK_ESCAPE)

    def foreground_matches(self, hwnd: int) -> bool:
        from app.device.windows import foreground_matches

        return foreground_matches(hwnd)

    def activate(self, hwnd: int) -> bool:
        from app.device.windows import activate_window

        return activate_window(hwnd)

    def window_usable(self, hwnd: int) -> bool:
        from app.device.windows import window_usable

        return window_usable(hwnd)

    def cursor_pos(self) -> tuple[int, int]:
        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = _POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            # 读不到光标时视为未接管；窗口类故障由 window_usable 覆盖。
            return (0, 0)
        return int(point.x), int(point.y)

    def left_button_pressed(self) -> bool:
        return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8001)

    def esc_pressed(self) -> bool:
        return bool(_user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001)


def _injector_holds_button(mouse: object, now: float) -> bool:
    """注入器正处于按下态，或刚抬起不久，此时的左键信号来自笔迹本身。"""
    active = getattr(mouse, "button_active", None)
    if callable(active) and active():
        return True
    last = getattr(mouse, "last_button_mono", None)
    return bool(callable(last) and now - float(last()) < BUTTON_EXEMPT_S)


def evaluate_signals(
    handle: HandleLike,
    probe: GuardProbe,
    mouse: object,
    target: "DrawingTarget",
    cursor_takeover_px: float,
    focus_guard: bool,
) -> str | None:
    """单次评估全部守护信号，命中返回停止原因，否则 None。

    ESC 是 PC 侧急停键，全程有效（含倒计时），与目标类型无关；焦点/主权信号
    中，窗口类走 target（投屏/ADB 目标可按能力降级），光标与按键走 probe
    （PC 输入主权，只要注入仍发生在 PC 上就成立）。
    光标接管仅在两笔之间比较：此刻光标由注入器精确保有，笔画中途的偏差是
    注入本身的中间点。
    """
    if probe.esc_pressed():
        return "esc"
    if not focus_guard or not handle.guard_active.is_set():
        return None

    if not target.window_usable() or not target.foreground_matches():
        return "focus_lost"

    # 每轮都读一次左键以消费 GetAsyncKeyState 的边定位：注入点击发生在豁免窗内，
    # 若窗内不读，边位会滞留到豁免结束后被当作「用户按下」。真实接管不受影响：
    # 持续按住时高位恒为 1，快速点击则边位在下一个轮询周期即被读到。
    pressed = probe.left_button_pressed()
    if pressed and not _injector_holds_button(mouse, time.monotonic()):
        return "mouse_takeover"

    settled = handle.settled_cursor
    if settled is not None and not handle.stroking.is_set():
        cx, cy = probe.cursor_pos()
        if math.hypot(cx - settled[0], cy - settled[1]) > cursor_takeover_px:
            return "mouse_takeover"
    return None
