"""画板下方工具面板的开合控制。

笔粗细滑杆只在面板展开时可点；绘制期间面板遮挡画布下部，用户希望默认收起、
仅在换笔时短暂展开。开合状态从截图直接判定：

  1. 折叠后画布可能扩大盖住滑杆区域——滑杆中心落在当前检测出的画布内 ⇒ 折叠；
  2. 否则按滑杆轨道的墨迹占比：展开时轨道有深色段+圆点（占比 ≥3%），
     收起后该位置是浅色空白（≈0），阈值取 1%。

「折叠」「展开」是外表不同、功能相反的两个独立按钮：折叠钮只在展开态显示，
展开钮只在收起态显示，各自只在对应状态下可点。因此 fold/unfold 接收的是
方向候选链（首选本方向按钮，次选对侧按钮——对侧按钮只在当前态恰好等于其
生效态时才生效，天然安全），逐钮尝试直到状态翻转。

安全约束：点击前校验目标点不在画布内（防止在画布上点出墨点），点击后用状态
复检确认生效；失败由调用方降级处理（保持当前笔宽继续画），绝不盲连点。
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Protocol

from app.core.vision.canvas_detect import detect_canvas_rect
from app.executor.ink_measure import ink_occupied_fraction

# 滑杆轨道深色占比阈值：展开时深色段+圆点 ≥3%，收起后≈0，取中间值。
PANEL_OPEN_OCCUPIED = 0.01
SETTLE_S = 0.45
# 单个按钮候选的点击预算：点一下不翻转即判该钮在当前态无效，换下一个候选。
CLICKS_PER_BUTTON = 2


class PanelProbe(Protocol):
    """DrawingTarget 的图像视角适配：capture_image 直接给截图 ndarray。"""

    def capture_image(self): ...
    def foreground_matches(self) -> bool: ...
    def activate(self) -> bool: ...


class PanelController:
    """按截图坐标操作折叠按钮；鼠标动作前经 to_screen 换算到屏幕坐标。"""

    def __init__(
        self,
        *,
        probe: PanelProbe,
        mouse: object,
        to_screen: Callable[[float, float], tuple[float, float]],
        sleep: Callable[[float], None] = time.sleep,
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        self._probe = probe
        self._mouse = mouse
        self._to_screen = to_screen
        self._sleep = sleep
        # 停止意图：开合一次要点 1-2 下、每下都截图+等待，慢环境里累计可达数秒；
        # 期间用户 ESC/最小化必须能立刻打断（含不再把用户刚切走的窗口抢回来）。
        self._stopped = stopped

    def is_open(self, slider_rect: dict) -> bool | None:
        image = self._probe.capture_image()
        if image is None:
            return None
        found = detect_canvas_rect(image)
        cx = slider_rect["x"] + slider_rect["w"] / 2.0
        cy = slider_rect["y"] + slider_rect["h"] / 2.0
        if found is not None:
            l, t, r, b = found
            if l <= cx <= r and t <= cy <= b:
                # 画布已盖住滑杆：只能是折叠态，此时区域内的墨迹是绘制笔画。
                return False
        rect = (slider_rect["x"], slider_rect["y"], slider_rect["w"], slider_rect["h"])
        return ink_occupied_fraction(image, rect) >= PANEL_OPEN_OCCUPIED

    def fold(self, slider_rect: dict, buttons: Iterable[dict | None]) -> bool:
        """收起面板。首选展开态才显示的「折叠」钮，次选「展开」钮兜底。"""
        return self._set(slider_rect, buttons, want_open=False)

    def unfold(self, slider_rect: dict, buttons: Iterable[dict | None]) -> bool:
        """展开面板。首选收起态才显示的「展开」钮，次选「折叠」钮兜底。"""
        return self._set(slider_rect, buttons, want_open=True)

    def _set(
        self, slider_rect: dict, buttons: Iterable[dict | None], *, want_open: bool
    ) -> bool:
        candidates = [p for p in buttons if p is not None]
        if not candidates:
            return False
        for button in candidates:
            if self._set_with(slider_rect, button, want_open=want_open):
                return True
        if self._stopped():
            return False
        state = self.is_open(slider_rect)
        return state is not None and state == want_open

    def _set_with(self, slider_rect: dict, button: dict, *, want_open: bool) -> bool:
        """对一个按钮候选尝试到预算耗尽；翻转成功返回 True。"""
        for _ in range(CLICKS_PER_BUTTON):
            if self._stopped():
                return False
            state = self.is_open(slider_rect)
            if state is None:
                return False
            if state == want_open:
                return True
            if self._stopped():
                return False
            image = self._probe.capture_image()
            if image is None:
                return False
            found = detect_canvas_rect(image)
            if found is not None:
                l, t, r, b = found
                if l <= button["x"] <= r and t <= button["y"] <= b:
                    # 按钮位置被（漂移后的）画布覆盖：点下去就是一个墨点，换候选。
                    return False
            self._ensure_foreground()
            sx, sy = self._to_screen(button["x"], button["y"])
            getattr(self._mouse, "click")(sx, sy)
            self._sleep(SETTLE_S)
        return False

    def _ensure_foreground(self) -> None:
        # 已请求停止时绝不 activate：那会把用户刚切走/最小化的窗口抢回前台，
        # 前台反复易手会让守护的 focus_lost 判定迟迟不成立，停笔被拖成秒级。
        if self._stopped() or self._probe.foreground_matches():
            return
        self._probe.activate()
        self._sleep(0.15)
