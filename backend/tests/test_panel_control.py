"""PanelController 仿真测试：假画布模拟「展开=画布小+轨道有墨，折叠=画布盖住滑杆」。"""

from __future__ import annotations

import numpy as np

from app.executor.panel_control import MAX_CLICKS, PanelController

SIZE = (700, 600)  # (h, w)
CANVAS_OPEN = (40, 60, 340, 460)  # l, t, r, b
CANVAS_FOLDED = (40, 60, 340, 560)  # 折叠后画布下扩，盖住滑杆
TRACK = {"x": 100, "y": 520, "w": 200, "h": 12}
FOLD = {"x": 60, "y": 580}


class FakePanelBoard:
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


class FakeMouse:
    def __init__(self, board: FakePanelBoard) -> None:
        self.board = board

    def click(self, x: float, y: float) -> None:
        self.board.clicks.append((x, y))
        board = self.board
        if board.toggle_enabled and abs(x - FOLD["x"]) < 20 and abs(y - FOLD["y"]) < 20:
            board.open = not board.open


def _controller(board: FakePanelBoard) -> PanelController:
    return PanelController(
        probe=board,
        mouse=FakeMouse(board),
        to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None,
    )


def test_is_open_true_when_panel_expanded() -> None:
    board = FakePanelBoard(open_=True)
    assert _controller(board).is_open(TRACK) is True


def test_is_open_false_when_canvas_covers_slider() -> None:
    # 折叠后画布盖住滑杆，即使区域内有绘制墨迹也必须判为折叠。
    board = FakePanelBoard(open_=False, ink_on_track=True)
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
        probe=DeadProbe(), mouse=FakeMouse(FakePanelBoard(open_=True)),
        to_screen=lambda x, y: (x, y), sleep=lambda _s: None,
    )
    assert ctrl.is_open(TRACK) is None


def test_fold_clicks_once_and_stops_when_already_folded() -> None:
    board = FakePanelBoard(open_=True)
    ctrl = _controller(board)
    assert ctrl.fold(TRACK, FOLD) is True
    assert len(board.clicks) == 1
    assert ctrl.fold(TRACK, FOLD) is True  # 已折叠：无需再点
    assert len(board.clicks) == 1


def test_unfold_clicks_once() -> None:
    board = FakePanelBoard(open_=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, FOLD) is True
    assert board.clicks == [(FOLD["x"], FOLD["y"])]


def test_unfold_refuses_click_inside_canvas() -> None:
    # 画布漂移后盖住折叠按钮：点下去会落墨，必须拒绝且零点击。
    board = FakePanelBoard(open_=False, canvas_override=(40, 60, 360, 620))
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, FOLD) is False
    assert board.clicks == []


def test_unfold_gives_up_after_max_clicks() -> None:
    board = FakePanelBoard(open_=False, toggle_enabled=False)
    ctrl = _controller(board)
    assert ctrl.unfold(TRACK, FOLD) is False
    assert len(board.clicks) == MAX_CLICKS


def test_stopped_aborts_before_any_click_or_activate() -> None:
    # 用户已请求停止：绝不点击、绝不把用户刚切走的窗口抢回前台——反复抢前台
    # 会让守护的 focus_lost 判定迟迟不成立，停笔被拖成秒级。
    class StealingBoard(FakePanelBoard):
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
        probe=board, mouse=FakeMouse(board), to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None, stopped=lambda: True,
    )
    assert ctrl.unfold(TRACK, FOLD) is False
    assert board.clicks == []
    assert board.activations == 0


def test_stop_during_settle_wins_over_further_clicks() -> None:
    # 第一次点击后用户按下停止：settle 立即醒来且不再补第二轮点击。
    board = FakePanelBoard(open_=False, toggle_enabled=False)
    state = {"stop": False}

    def settle(_seconds: float) -> None:
        state["stop"] = True

    ctrl = PanelController(
        probe=board, mouse=FakeMouse(board), to_screen=lambda x, y: (x, y),
        sleep=settle, stopped=lambda: state["stop"],
    )
    assert ctrl.unfold(TRACK, FOLD) is False
    assert len(board.clicks) == 1
