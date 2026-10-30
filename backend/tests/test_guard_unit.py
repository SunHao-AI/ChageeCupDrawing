"""evaluate_signals 的纯逻辑单元测试：验证判定语义与误判防护。"""

from __future__ import annotations

import threading

import pytest

from app.executor.guard import BUTTON_EXEMPT_S, CONFIRM_HITS, GuardState, evaluate_signals

pytest.importorskip("ctypes", reason="守护模块依赖 ctypes")

from app.executor.guard import HandleLike  # noqa: E402


class StubHandle:
    def __init__(self, guard_active: bool, stroking: bool = False, settled=None) -> None:
        self.guard_active = threading.Event()
        self.stroking = threading.Event()
        self.settled_cursor = settled
        if guard_active:
            self.guard_active.set()
        if stroking:
            self.stroking.set()


class StubProbe:
    """PC 输入侧信号假实现（ESC/左键/光标），与目标类型无关。"""

    def __init__(self, **flags) -> None:
        self.flags = {
            "esc": False,
            "space": False,
            "lbtn": False,
            "held": False,
            "cursor": (100, 100),
            **flags,
        }

    def clear_escape_state(self) -> None:
        return None

    def esc_pressed(self) -> bool:
        return self.flags["esc"]

    def space_pressed(self) -> bool:
        return self.flags["space"]

    def left_button_pressed(self) -> bool:
        return self.flags["lbtn"]

    def left_button_held(self) -> bool:
        return self.flags["held"]

    def cursor_pos(self) -> tuple[int, int]:
        return self.flags["cursor"]


class StubTarget:
    """DrawingTarget 假实现：只实现守护用到的焦点类信号。"""

    kind = "stub"

    def __init__(self, fg: bool = True, usable: bool = True) -> None:
        self.fg = fg
        self.usable = usable

    def foreground_matches(self) -> bool:
        return self.fg

    def window_usable(self) -> bool:
        return self.usable

    def alive(self) -> bool:
        return self.usable

    def activate(self) -> bool:
        return False

    def capture(self):
        raise NotImplementedError

    def capture_image(self):
        raise NotImplementedError

    def client_area(self):
        raise NotImplementedError


def _target(probe: StubProbe):
    # 兼容旧写法：从 StubProbe 的 fg/usable 标志生成目标桩。
    return StubTarget(fg=probe.flags.get("fg", True), usable=probe.flags.get("usable", True))


class StubMouse:
    def __init__(self, active: bool = False, last_up: float | None = None) -> None:
        self._active = active
        self._last_up = last_up
        self.releases = 0

    def button_active(self) -> bool:
        return self._active

    def last_button_mono(self) -> float:
        return self._last_up if self._last_up is not None else 0.0

    def up(self) -> None:
        self.releases += 1


def _eval(handle, probe, mouse=None, takeover_px=6.0, focus_guard=True, target=None, state=None):
    return evaluate_signals(
        handle, probe, mouse or StubMouse(),
        target if target is not None else _target(probe),
        takeover_px, focus_guard, state if state is not None else GuardState(),
    )


def test_esc_wins_and_works_in_arming() -> None:
    handle = StubHandle(guard_active=False)
    probe = StubProbe(esc=True, fg=False)
    assert _eval(handle, probe) == "esc"


def test_space_pauses_even_in_arming() -> None:
    handle = StubHandle(guard_active=False)
    probe = StubProbe(space=True, fg=False)
    assert _eval(handle, probe) == "pause"


def test_esc_takes_priority_over_space() -> None:
    handle = StubHandle(guard_active=False)
    assert _eval(handle, StubProbe(esc=True, space=True)) == "esc"


def test_win32_probe_implements_every_protocol_method() -> None:
    """真实探针必须实现 GuardProbe 的全部信号方法。

    契约测试全程用假探针，真实类漏一个方法（如 space_pressed）要到真机
    AttributeError 才暴露——这里在属性层面直接封堵。
    """
    from app.executor.guard import Win32GuardProbe

    probe = Win32GuardProbe()
    for name in (
        "clear_escape_state", "foreground_matches", "activate", "window_usable",
        "cursor_pos", "left_button_pressed", "left_button_held",
        "esc_pressed", "space_pressed",
    ):
        assert callable(getattr(probe, name, None)), f"Win32GuardProbe 缺少 {name}"


def test_focus_signals_only_after_drawing_started() -> None:
    handle = StubHandle(guard_active=False)
    probe = StubProbe(fg=False, usable=False, lbtn=True, settled=None)
    handle.settled_cursor = (0, 0)
    probe.flags["cursor"] = (999, 999)
    assert _eval(handle, probe) is None


def test_focus_guard_off_skips_everything_but_esc() -> None:
    handle = StubHandle(guard_active=True, settled=(0, 0))
    probe = StubProbe(fg=False, lbtn=True, cursor=(999, 999))
    assert _eval(handle, probe, focus_guard=False) is None
    assert _eval(handle, StubProbe(space=True), focus_guard=False) == "pause"


def test_foreground_lost_maps_to_focus_lost() -> None:
    assert _eval(StubHandle(True), StubProbe(fg=False)) == "focus_lost"
    assert _eval(StubHandle(True), StubProbe(usable=False)) == "focus_lost"


def test_injected_button_activity_is_exempt() -> None:
    handle = StubHandle(True)
    probe = StubProbe(lbtn=True)
    assert _eval(handle, probe, StubMouse(active=True)) is None
    import time

    just_up = time.monotonic() - BUTTON_EXEMPT_S / 2
    assert _eval(handle, probe, StubMouse(last_up=just_up)) is None


def test_user_click_while_idle_is_takeover() -> None:
    handle = StubHandle(True)
    probe = StubProbe(lbtn=True)
    stale_up = 0.0
    # 完整的一次「按下又抬起」（边位还在、高位已掉）：即刻停笔。
    assert _eval(handle, probe, StubMouse(last_up=stale_up)) == "mouse_takeover"


def test_phantom_button_self_heals_before_stop() -> None:
    """注入器早已抬笔、系统却仍报按住（LEFTUP 丢失）：先补发 LEFTUP 而不是停笔。"""
    import time

    handle = StubHandle(True)
    probe = StubProbe(lbtn=True, held=True)
    mouse = StubMouse(last_up=time.monotonic() - 0.5)
    state = GuardState()
    assert _eval(handle, probe, mouse, state=state) is None
    assert mouse.releases == 1
    # 补发后系统不再报按住 → 自愈成功。
    probe.flags.update({"lbtn": False, "held": False})
    assert _eval(handle, probe, mouse, state=state) is None


def test_real_button_hold_stops_after_confirm() -> None:
    """真长按（补发 LEFTUP 无效）：连续 CONFIRM_HITS 轮后停笔。"""
    import time

    handle = StubHandle(True)
    probe = StubProbe(lbtn=True, held=True)
    mouse = StubMouse(last_up=time.monotonic() - 5.0)  # 远超悬挂归因窗
    state = GuardState()
    for _ in range(CONFIRM_HITS - 1):
        assert _eval(handle, probe, mouse, state=state) is None
    assert _eval(handle, probe, mouse, state=state) == "mouse_takeover"
    assert state.evidence["trigger"] == "button_hold"


def test_cursor_deviation_checked_only_between_strokes() -> None:
    probe = StubProbe(cursor=(100, 100))
    mid_stroke = StubHandle(True, stroking=True, settled=(10, 10))
    assert _eval(mid_stroke, probe) is None  # 笔画中途不比

    settled = StubHandle(True, settled=(10, 10))
    state = GuardState()
    # 单次间隙采样超阈值不再即停：一次性跳变（远控/跳光标）会被下一笔注入
    # 拉回，须连续 CONFIRM_HITS 个间隙仍超阈值才判接管。
    for _ in range(CONFIRM_HITS - 1):
        assert _eval(settled, probe, state=state) is None
    assert _eval(settled, probe, state=state) == "mouse_takeover"
    assert state.evidence["trigger"] == "cursor"
    assert state.evidence["cursorOffsetPx"] > 6.0

    # 偏移回落后计数清零（跳变被注入拉回的情形）。
    settled2 = StubHandle(True, settled=(100, 100))
    state2 = GuardState()
    assert _eval(settled2, probe, state=state2) is None
    probe.flags["cursor"] = (10, 10)
    assert _eval(settled2, probe, state=state2) is None

    within = StubProbe(cursor=(14, 14))
    assert _eval(settled, within) is None  # 阈值内不误报

    never_placed = StubHandle(True, settled=None)
    assert _eval(never_placed, probe) is None
