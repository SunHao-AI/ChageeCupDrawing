"""Win32 鼠标注入，迁移自 V6 的 NativeMouse。

笔画内移动用 SendInput + MOUSEEVENTF_MOVE_NOCOALESCE（Windows 默认会合并
WM_MOUSEMOVE，画布按移动事件采样时长直线会丢中间点）；笔画间跳转用
SetCursorPos，避免留下密集移动样本。
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from collections.abc import Callable, Sequence

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_ulong),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def path_length(points: Sequence[tuple[float, float]]) -> float:
    return sum(
        math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


class NativeMouse:
    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_ABSOLUTE = 0x8000
    VK_ESCAPE = 0x1B
    VK_SPACE = 0x20

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("自动鼠标绘制仅支持 Windows。")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
        self.user32.SendInput.restype = ctypes.c_uint
        self._button_active = False
        self._last_up_mono = 0.0

    # ---------- 基础 ----------

    def position(self) -> tuple[int, int]:
        point = self._POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError()
        return int(point.x), int(point.y)

    def move_to(self, x: float, y: float) -> None:
        if not self.user32.SetCursorPos(int(round(x)), int(round(y))):
            raise ctypes.WinError()

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        # SM_XVIRTUALSCREEN..SM_CYVIRTUALSCREEN = 76..79
        vx = int(self.user32.GetSystemMetrics(76))
        vy = int(self.user32.GetSystemMetrics(77))
        vw = max(1, int(self.user32.GetSystemMetrics(78)))
        vh = max(1, int(self.user32.GetSystemMetrics(79)))
        return vx, vy, vw, vh

    def _send_mouse(self, flags: int, x: float | None = None, y: float | None = None) -> None:
        dx = dy = 0
        if x is not None and y is not None:
            vx, vy, vw, vh = self._virtual_screen()
            dx = int(round((float(x) - vx) * 65535.0 / max(1, vw - 1)))
            dy = int(round((float(y) - vy) * 65535.0 / max(1, vh - 1)))
            dx = max(0, min(65535, dx))
            dy = max(0, min(65535, dy))
            flags |= self.MOUSEEVENTF_ABSOLUTE | self.MOUSEEVENTF_VIRTUALDESK
        payload = _INPUT(type=self.INPUT_MOUSE)
        payload.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        if self.user32.SendInput(1, ctypes.byref(payload), ctypes.sizeof(_INPUT)) != 1:
            raise ctypes.WinError()

    def move_sample(self, x: float, y: float, no_coalesce: bool = True) -> None:
        flags = self.MOUSEEVENTF_MOVE
        if no_coalesce:
            flags |= self.MOUSEEVENTF_MOVE_NOCOALESCE
        self._send_mouse(flags, x, y)

    def down(self) -> None:
        self._button_active = True
        self._send_mouse(self.MOUSEEVENTF_LEFTDOWN)

    def up(self) -> None:
        # 先置状态再发事件：守护线程读到按下位时豁免已生效。
        self._button_active = False
        self._last_up_mono = time.monotonic()
        self._send_mouse(self.MOUSEEVENTF_LEFTUP)

    def button_active(self) -> bool:
        return self._button_active

    def last_button_mono(self) -> float:
        return self._last_up_mono

    def click(self, x: float, y: float, hold: float = 0.020) -> None:
        self.move_to(x, y)
        self.down()
        time.sleep(max(0.010, hold))
        self.up()
        time.sleep(0.004)

    def esc_pressed(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(self.VK_ESCAPE) & 0x8001)

    def space_pressed(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(self.VK_SPACE) & 0x8001)

    def clear_escape_state(self) -> None:
        # 再读一次以清除「自上次调用后被按下」的低位置位（ESC 与空格同为急停键）。
        self.user32.GetAsyncKeyState(self.VK_ESCAPE)
        self.user32.GetAsyncKeyState(self.VK_SPACE)

    # ---------- 落笔 ----------

    def drag_polyline(
        self,
        points: Sequence[tuple[float, float]],
        *,
        event_step_px: float = 1.0,
        point_delay_s: float = 0.0015,
        should_stop: Callable[[], bool],
        no_coalesce: bool = True,
        short_stroke_protect: bool = True,
    ) -> bool:
        """按住左键沿折线移动；被停止打断时保证松笔并返回 False。"""
        if not points:
            return True
        if should_stop():
            return False
        if len(points) == 1:
            self.click(points[0][0], points[0][1], hold=0.022)
            return not should_stop()

        plen = path_length(points)
        effective_delay = max(0.0004, float(point_delay_s))
        if short_stroke_protect and plen < 14.0:
            effective_delay = max(effective_delay, 0.0035)

        self.move_to(*points[0])
        self.down()
        completed = True
        try:
            # 给目标画布一个真实的落笔样本再开始移动。
            time.sleep(0.010 if short_stroke_protect else 0.004)
            prev_x, prev_y = points[0]
            for x, y in points[1:]:
                if should_stop():
                    completed = False
                    break
                dist = math.hypot(x - prev_x, y - prev_y)
                steps = max(1, int(math.ceil(dist / max(0.65, event_step_px))))
                for i in range(1, steps + 1):
                    if should_stop():
                        completed = False
                        break
                    t = i / steps
                    self.move_sample(
                        prev_x + (x - prev_x) * t,
                        prev_y + (y - prev_y) * t,
                        no_coalesce=no_coalesce,
                    )
                    if effective_delay:
                        time.sleep(effective_delay)
                if not completed:
                    break
                prev_x, prev_y = x, y
            if completed:
                # 末尾短驻留，避免最后 1-2 px 丢失。
                time.sleep(0.008 if short_stroke_protect and plen < 14.0 else 0.003)
        finally:
            self.up()
        return completed
