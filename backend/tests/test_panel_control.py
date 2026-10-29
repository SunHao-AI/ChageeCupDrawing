"""PanelController 仿真测试。

两种画板形态：
  - ToggleBoard：可逆单钮（同一按钮两态互切，向后兼容的旧 UI）；
  - DualButtonBoard：「折叠」「展开」是两个独立按钮，折叠钮只在展开态生效、
    展开钮只在收起态生效（真实画板形态）。
「展开=画布小+轨道有墨，折叠=画布盖住滑杆」的状态判定两板一致。
"""

from __future__ import annotations

import numpy as np

from app.executor.panel_control import CLICKS_PER_BUTTON, PanelController

SIZE = (700, 600)  # (h, w)
CANVAS_OPEN = (40, 60, 340, 460)  # l, t, r, b
CANVAS_FOLDED = (40, 60, 340, 560)  # 折叠后画布下扩，盖住滑杆
TRACK = {"x": 100, "y": 520, "w": 200, "h": 12}
FOLD = {"x": 60, "y": 580}  # 折叠钮（展开态生效）
UNFOLD = {"x": 320, "y": 580}  # 展开钮（收起态生效，独立位置；画布外才允许点击）


class ToggleBoard:
    """可逆单钮：FOLD 位置在任何态都开合面板。"""

    def __init__(
        self,
        *,
        open_: bool,
        toggle_enabled: bool = True,
        canvas_override: tuple[int, int, int, int] | None = None,
        ink_on_track: bool = False,
    ) -> None:
        self.open = open_
        self.toggle_enabled = toggle_enabled
        self.canvas_override = canvas_override
        self.ink_on_track = ink_on_track
        self.clicks: list[tuple[float, float]] = []

    def capture_image(self) -> np.ndarray:
        grid = np.full(SIZE, 240, dtype=np.uint8)
        canvas = self.canvas_override or (CANVAS_OPEN if self.open else CANVAS_FOLDED)
        l, t, r, b = canvas
        grid[t:b, l:r] = 255
        cy = TRACK["y"] + TRACK["h"] // 2
        if self.open or self.ink_on_track:
            grid[cy - 2:cy + 3, TRACK["x"]:TRACK["x"] + TRACK["w"]] = 0
        return grid

    def foreground_matches(self) -> bool:
        return True

    def activate(self) -> bool:
        return True

    def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        if self.toggle_enabled and abs(x - FOLD["x"]) < 20 and abs(y - FOLD["y"]) < 20:
            self.open = not self.open


class DualButtonBoard(ToggleBoard):
    """两个独立按钮：折叠钮只在展开态生效，展开钮只在收起态生效。"""

    def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        if self.open and abs(x - FOLD["x"]) < 20 and abs(y - FOLD["y"]) < 20:
            self.open = False
            return
        if not self.open and abs(x - UNFOLD["x"]) < 20 and abs(y - UNFOLD["y"]) < 20:
            self.open = True


def _controller(board, **kwargs) -> PanelController:
    return PanelController(
        probe=board,
        mouse=board,
        to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None,
        **kwargs,
    )


def test_is_open_true_when_panel_expanded() -> None:
    board = ToggleBoard(open_=True)
    assert _controller(board).is_open(TRACK) is True


def test_is_open_false_when_canvas_covers_slider() -> None:
    # 折叠后画布盖住滑杆，即使区域内有绘制墨迹也必须判为折叠。
    board = ToggleBoard(open_=False, ink_on_track=True)
    assert _controller(board).is_open(TRACK) is False


def test_is_open_none_when_capture_fails() -> None:
    class DeadProbe:
        def capture_image(self):
            return None

        def foreground_matches(self) -> bool:
            return True

        def activate(self) -> bool:
            return True

    ctrl = PanelController(
        probe=DeadProbe(), mouse=ToggleBoard(open_=True),
        to_screen=lambda x, y: (x, y), sleep=lambda _s: None,
    )
    assert ctrl.is_open(TRACK) is None


# ---------- 可逆单钮（旧 UI 向后兼容） ----------


def test_fold_clicks_once_and_stops_when_already_folded() -> None:
    board = ToggleBoard(open_=True)
    ctrl = _controller(board)
    assert ctrl.fold(TRACK, [FOLD]) is True
    assert len(board.clicks) == 1
    assert ctrl.fold(TRACK, [FOLD]) is True  # 已折叠：无需再点
    assert len(board.clicks) == 1


def test_unfold_clicks_once() -> None:
    board = ToggleBoard(open_=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [FOLD]) is True
    assert board.clicks == [(FOLD["x"], FOLD["y"])]


def test_unfold_refuses_click_inside_canvas() -> None:
    # 画布漂移后盖住折叠按钮：点下去会落墨，必须拒绝且零点击。
    board = ToggleBoard(open_=False, canvas_override=(40, 60, 360, 620))
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [FOLD]) is False
    assert board.clicks == []


def test_unfold_gives_up_after_per_button_budget() -> None:
    board = ToggleBoard(open_=False, toggle_enabled=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [FOLD]) is False
    assert len(board.clicks) == CLICKS_PER_BUTTON


# ---------- 两个独立按钮（真实画板形态） ----------


def test_dual_buttons_use_directional_button() -> None:
    # 收起→展开点展开钮；展开→收起点折叠钮；绝不点反方向的钮。
    board = DualButtonBoard(open_=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [UNFOLD, FOLD]) is True
    assert board.clicks == [(UNFOLD["x"], UNFOLD["y"])]
    assert ctrl.fold(TRACK, [FOLD, UNFOLD]) is True
    assert board.clicks[-1] == (FOLD["x"], FOLD["y"])


def test_dual_button_wrong_direction_button_gives_up_not_blind_click() -> None:
    # 只给折叠钮时无法展开：预算内点折叠钮（收起态无效）后放弃，绝不无限连点。
    board = DualButtonBoard(open_=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [FOLD]) is False
    assert len(board.clicks) == CLICKS_PER_BUTTON
    assert board.open is False


def test_dual_button_fallback_second_candidate_rescues() -> None:
    # 首选钮在当前态无效（如状态误判），链上第二个钮生效即成功。
    board = DualButtonBoard(open_=True)
    ctrl = _controller(board)
    assert ctrl.fold(TRACK, [UNFOLD, FOLD]) is True
    assert board.open is False
    assert (UNFOLD["x"], UNFOLD["y"]) in board.clicks
    assert (FOLD["x"], FOLD["y"]) in board.clicks


def test_empty_button_chain_fails_without_click() -> None:
    board = ToggleBoard(open_=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, [None]) is False
    assert board.clicks == []


# ---------- 停止意图 ----------


def test_stopped_aborts_before_any_click_or_activate() -> None:
    # 用户已请求停止：绝不点击、绝不把用户刚切走的窗口抢回前台——反复抢前台
    # 会让守护的 focus_lost 判定迟迟不成立，停笔被拖成秒级。
    class StealingBoard(ToggleBoard):
        def __init__(self) -> None:
            super().__init__(open_=False)
            self.activations = 0

        def foreground_matches(self) -> bool:
            return False

        def activate(self) -> bool:
            self.activations += 1
            return True

    board = StealingBoard()
    ctrl = PanelController(
        probe=board, mouse=board, to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None, stopped=lambda: True,
    )
    assert ctrl.unfold(TRACK, [FOLD]) is False
    assert board.clicks == []
    assert board.activations == 0


def test_stop_during_settle_wins_over_further_clicks() -> None:
    # 第一次点击后用户按下停止：settle 立即醒来且不再补第二轮点击。
    board = ToggleBoard(open_=False, toggle_enabled=False)
    state = {"stop": False}

    def settle(_seconds: float) -> None:
        state["stop"] = True

    ctrl = PanelController(
        probe=board, mouse=board, to_screen=lambda x, y: (x, y),
        sleep=settle, stopped=lambda: state["stop"],
    )
    assert ctrl.unfold(TRACK, [FOLD]) is False
    assert len(board.clicks) == 1
