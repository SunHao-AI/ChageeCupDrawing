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
VK_SPACE = 0x20
# 注入器 up 之后该时段内读到的左键按下位来自笔迹本身，不算用户接管。
BUTTON_EXEMPT_S = 0.15
# 长按接管确认所需的连续命中轮数：真长按会持续命中，瞬时干扰不会。代价是停笔
# 晚一个轮询周期（默认 50ms）。已抬起的「完整一次点击」走边位分支，仍即刻停笔。
CONFIRM_HITS = 2
# 「悬挂按下位」归因窗口：注入器抬笔该时段内读到「系统说按住、注入器说没按」，
# 优先怀疑是我们自己的 LEFTUP 丢了（补发一次自愈），而非用户长按。
PHANTOM_BUTTON_WINDOW_S = 1.0

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class GuardProbe(Protocol):
    """守护信号的数据源；测试以假实现替换，避免依赖真实桌面状态。"""

    def clear_escape_state(self) -> None: ...

    def foreground_matches(self, hwnd: int) -> bool: ...

    def activate(self, hwnd: int) -> bool: ...

    def window_usable(self, hwnd: int) -> bool: ...

    def cursor_pos(self) -> tuple[int, int]: ...

    def left_button_pressed(self) -> bool: ...

    def left_button_held(self) -> bool: ...

    def esc_pressed(self) -> bool: ...

    def space_pressed(self) -> bool: ...


class HandleLike(Protocol):
    """_RunHandle 的守护视角：阶段标志 + 静置光标位置。"""

    guard_active: threading.Event
    stroking: threading.Event
    settled_cursor: tuple[int, int] | None


class GuardState:
    """跨轮询周期的接管判定状态：连续命中计数 + 最后一次判据读数。

    单轮信号不足以定接管（触控板轻碰、外部程序一次绝对定位、SendInput 丢一个
    LEFTUP 都只存在一两轮），须连续命中才停笔；证据留在 ``evidence``，停笔时
    随 task.done 上报，供事后区分「真接管」与「误判」。
    """

    def __init__(self) -> None:
        self.button_hits = 0
        self.cursor_hits = 0
        # 悬挂按下位是否已补发过 LEFTUP（只在连续按住时累进）。
        self.phantom_fix_sent = False
        self.evidence: dict = {}


class Win32GuardProbe:
    def clear_escape_state(self) -> None:
        # 读一次消费「自上次读取以来被按下」的边定位；空格同属急停键，一并清。
        _user32.GetAsyncKeyState(VK_ESCAPE)
        _user32.GetAsyncKeyState(VK_SPACE)

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

    def left_button_held(self) -> bool:
        # 只看「当前按下」高位，不含 bit0 边位：边位会让一次陈旧按下被当成按住。
        return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)

    def esc_pressed(self) -> bool:
        return bool(_user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001)

    def space_pressed(self) -> bool:
        return bool(_user32.GetAsyncKeyState(VK_SPACE) & 0x8001)


def _injector_holds_button(mouse: object, now: float) -> bool:
    """注入器正处于按下态，或刚抬起不久，此时的左键信号来自笔迹本身。"""
    active = getattr(mouse, "button_active", None)
    if callable(active) and active():
        return True
    last = getattr(mouse, "last_button_mono", None)
    return bool(callable(last) and now - float(last()) < BUTTON_EXEMPT_S)


def _injector_button_down(mouse: object) -> bool:
    active = getattr(mouse, "button_active", None)
    return bool(callable(active) and active())


def _last_up_mono(mouse: object, now: float) -> float:
    last = getattr(mouse, "last_button_mono", None)
    return float(last()) if callable(last) else now


def _release_button(mouse: object) -> None:
    """补发一次 LEFTUP：SendInput 丢 up 会让系统按键状态悬挂。"""
    up = getattr(mouse, "up", None)
    if callable(up):
        up()


def evaluate_signals(
    handle: HandleLike,
    probe: GuardProbe,
    mouse: object,
    target: "DrawingTarget",
    cursor_takeover_px: float,
    focus_guard: bool,
    state: GuardState | None = None,
) -> str | None:
    """单次评估全部守护信号，命中返回停止原因，否则 None。

    ESC / 空格是 PC 侧急停键，全程有效（含倒计时），与目标类型无关；焦点/主权
    信号中，窗口类走 target（投屏/ADB 目标可按能力降级），光标与按键走 probe
    （PC 输入主权，只要注入仍发生在 PC 上就成立）。

    两条接管判据的误判防护（真接管会持续命中，瞬时干扰不会）：
    - 物理左键：「按下又抬起的一次完整点击」仍即刻停笔（边位分支）；只有
      「当前按住」高位才计数，且注入器自认早已抬笔时先补发一次 LEFTUP 自愈
      （SendInput 丢 up 会让系统按键状态悬挂，旧实现豁免一过即误判接管），
      补发后仍按住才按 ``CONFIRM_HITS`` 轮确认。
    - 光标偏移：笔画期间不比（中间点本来在高速移动），笔画一结束就按「注入器
      以为的落点」比较。旧实现单轮超阈值即停，于是任何在笔画进行中把光标挪动
      >阈值的干扰（触控板轻碰、远控、系统跳光标）都会在下一笔结束瞬间必停——
      这是「用户没碰鼠标却停笔」的主因。新判据要求连续 ``CONFIRM_HITS`` 个
      间隙采样都超阈值：一次性跳变会被下一笔的注入定位拉回，下个间隙自然清零；
      用户持续拖动才连续命中。
    判定细节写入 state.evidence，随 task.done 的 stopEvidence 上报。
    """
    if probe.esc_pressed():
        return "esc"
    if probe.space_pressed():
        return "pause"
    if not focus_guard or not handle.guard_active.is_set():
        return None

    if not target.window_usable() or not target.foreground_matches():
        return "focus_lost"

    if state is None:
        state = GuardState()
    now = time.monotonic()

    # 每轮都读一次左键边位以消费之：注入点击在豁免窗内若不读，边位会滞留到
    # 豁免结束后被当作「用户按下」。
    pressed = probe.left_button_pressed()
    held = probe.left_button_held()
    exempt = _injector_holds_button(mouse, now)
    active = _injector_button_down(mouse)
    if not held:
        # 「按下又抬起」的完整一次点击（边位）＝ 用户点了鼠标，即刻停笔。
        state.phantom_fix_sent = False
        state.button_hits = 0
        if pressed and not exempt:
            state.evidence.update({"trigger": "button_click"})
            return "mouse_takeover"
    elif exempt or active:
        # 注入器自己按着（笔迹中/抬笔豁免窗内）：不算接管，顺带清悬挂标记。
        state.phantom_fix_sent = False
        state.button_hits = 0
    elif (
        now - _last_up_mono(mouse, now) < PHANTOM_BUTTON_WINDOW_S
        and not state.phantom_fix_sent
    ):
        # 注入器抬笔不久、系统却仍报按住：多半是我们的 LEFTUP 丢了，先补发自愈。
        # 抬笔早已超过归因窗口的按住则更可能是用户真长按，直接进计数。
        _release_button(mouse)
        state.phantom_fix_sent = True
        state.button_hits = 0
    else:
        # 补发之后仍按住 → 是真长按，按轮数确认后停笔。
        state.button_hits += 1
    last_up = _last_up_mono(mouse, now)
    state.evidence.update({
        "buttonPressed": pressed, "buttonHeld": held, "buttonExempt": exempt,
        "buttonHits": state.button_hits,
        "msSinceLastUp": round((now - last_up) * 1000) if last_up else None,
    })
    if state.button_hits >= CONFIRM_HITS:
        state.evidence.update({"trigger": "button_hold", "hits": state.button_hits})
        return "mouse_takeover"

    settled = handle.settled_cursor
    if settled is not None and not handle.stroking.is_set():
        cx, cy = probe.cursor_pos()
        dx = math.hypot(cx - settled[0], cy - settled[1])
        # 每条笔迹都以 SetCursorPos 起笔、以路径终点收笔，采样又只发生在两笔
        # 之间，故 dx 超阈值必然是「我们最后一次落笔之后」发生的位移。一次性
        # 跳变（远控/系统跳光标/触控板轻碰）下一笔的注入会把光标带回路径，下一
        # 个间隙采样自然回到阈值内 → 计数清零；用户持续拖动才会连续命中。
        if dx > cursor_takeover_px:
            state.cursor_hits += 1
        else:
            state.cursor_hits = 0
        state.evidence.update({
            "cursorOffsetPx": round(dx, 1), "cursorThresholdPx": cursor_takeover_px,
            "cursorHits": state.cursor_hits,
            "cursor": [cx, cy], "settled": list(settled),
        })
        if state.cursor_hits >= CONFIRM_HITS:
            state.evidence.update({"trigger": "cursor", "hits": state.cursor_hits})
            return "mouse_takeover"
    return None
