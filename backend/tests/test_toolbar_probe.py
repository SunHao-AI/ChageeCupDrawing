"""ToolbarProber 仿真测试：合成工具栏按 panel_open/tool 状态渲染，点击按控件矩形派发状态迁移。

画板只模拟行为（面板展开/折叠、工具切换、撤销、滑杆），不模拟图标内容，
与 ToolbarProber「点击→观测变化」的探测判据一一对应。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.executor.toolbar_probe import ProbeError, ToolBitmap, ToolbarProber

CANVAS = (40, 60, 300, 360)
BAND = (46, 70, 200, 40)
CANVAS_BOTTOM = CANVAS[1] + CANVAS[3]


class ToolbarBoard:
    """合成工具栏：grid 上画折叠钮/画笔格/橡皮格/撤销格；panel_open 决定
    滑杆是否可见与工具格亮度；handle_click 按控件矩形派发状态迁移。"""

    FOLD = (300, 500, 24, 24)
    BRUSH = (10, 495, 40, 40)
    ERASER = (60, 495, 40, 40)
    UNDO = (110, 495, 40, 40)
    SLIDER = (100, 530, 200, 12)
    SHIFT = 40  # 折叠态工具格整体上移量（折叠钮本身不动，以便再次点击切回）
    SELECTED_LUM = 180
    IDLE_LUM = 250

    def __init__(self) -> None:
        self.canvas_pixels = np.full((700, 600), 255, dtype=np.uint8)
        self.panel_open = True
        self.tool = "brush"
        self.stroke_width = 3.0
        self.strokes: list[list[tuple[int, int, int, int, np.ndarray]]] = []
        self.pending: list[tuple[int, int, int, int, np.ndarray]] = []
        self.clicks: list[tuple[float, float]] = []
        self.undos = 0

    # ---------- 状态渲染 ----------

    def _tool_rect(self, rect):
        x, y, w, h = rect
        if not self.panel_open:
            y -= self.SHIFT
        return x, y, w, h

    def _hit(self, rect, x: float, y: float) -> bool:
        x0, y0, w, h = rect
        return x0 <= x <= x0 + w and y0 <= y <= y0 + h

    def image(self) -> np.ndarray:
        img = self.canvas_pixels.copy()
        if self.panel_open:
            x, y, w, h = self.SLIDER
            img[y : y + h, x : x + w] = 0
        cells = (
            ("fold", self.FOLD, 235),
            ("brush", self._tool_rect(self.BRUSH), None),
            ("eraser", self._tool_rect(self.ERASER), None),
            ("undo", self._tool_rect(self.UNDO), 240),
        )
        for name, (x, y, w, h), fixed in cells:
            if name == "brush":
                lum = self.SELECTED_LUM if self.tool == "brush" else self.IDLE_LUM
            elif name == "eraser":
                lum = self.SELECTED_LUM if self.tool == "eraser" else self.IDLE_LUM
            else:
                lum = fixed
            img[y : y + h, x : x + w] = lum
        return img

    # ---------- 鼠标动作派发 ----------

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self.FOLD, x, y):
            self.panel_open = not self.panel_open
            return
        if self._hit(self._tool_rect(self.UNDO), x, y):
            self.undo()
            return
        if self._hit(self._tool_rect(self.BRUSH), x, y):
            self.tool = "brush"
            return
        if self._hit(self._tool_rect(self.ERASER), x, y):
            self.tool = "eraser"
            return
        if self.panel_open and self._hit(self.SLIDER, x, y):
            frac = (x - self.SLIDER[0]) / self.SLIDER[2]
            self.stroke_width = 3.0 + 22.0 * max(0.0, min(1.0, frac))

    def apply_stroke(self, x0: float, y0: float, x1: float, y1: float) -> None:
        width = max(1, int(round(self.stroke_width)))
        half = width // 2
        xs0, xs1 = int(min(x0, x1)), int(max(x0, x1)) + 1
        ys0, ys1 = int(min(y0, y1)) - half, int(max(y0, y1)) + half + 1
        region = self.canvas_pixels[ys0:ys1, xs0:xs1]
        prev = region.copy()
        region[:] = 0 if self.tool == "brush" else 255
        self.pending.append((ys0, ys1, xs0, xs1, prev))

    def undo(self) -> None:
        self.undos += 1
        if not self.strokes:
            return
        for ys0, ys1, xs0, xs1, prev in reversed(self.strokes.pop()):
            self.canvas_pixels[ys0:ys1, xs0:xs1] = prev


class ToolbarMouse:
    def __init__(self, board: ToolbarBoard) -> None:
        self.board = board
        self.pos = (0.0, 0.0)
        self.down_flag = False
        self.press_pos = (0.0, 0.0)

    def move_to(self, x, y):
        self.pos = (float(x), float(y))

    def move_sample(self, x, y, no_coalesce=True):
        if self.down_flag:
            self.board.apply_stroke(self.pos[0], self.pos[1], float(x), float(y))
        self.pos = (float(x), float(y))

    def down(self):
        self.board.handle_click(*self.pos)
        self.board.pending = []
        self.press_pos = self.pos
        self.down_flag = True

    def up(self):
        if self.board.pending:
            self.board.strokes.append(self.board.pending)
            self.board.pending = []
        else:
            # 无位移的按下-抬起才算一次「点击」；带位移的按下属于描线。
            self.board.clicks.append(self.press_pos)
        self.down_flag = False


class ToolbarProbe:
    def __init__(self, board: ToolbarBoard) -> None:
        self.board = board

    def capture_image(self):
        return self.board.image()

    def foreground_matches(self) -> bool:
        return True

    def activate(self) -> bool:
        return True


def _prober(board: ToolbarBoard) -> tuple[ToolbarProber, object]:
    probe = ToolbarProbe(board)
    mouse = ToolbarMouse(board)
    prober = ToolbarProber(
        probe=probe, mouse=mouse, to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None,
    )
    return prober, mouse


def _run(
    board: ToolbarBoard,
    *,
    open_bitmap: ToolBitmap | None = None,
    should_stop=lambda: False,
    progress: list[str] | None = None,
) -> ToolBitmap:
    prober, _ = _prober(board)
    return prober.run(
        canvas=CANVAS,
        band=BAND,
        open_bitmap=open_bitmap,
        on_progress=(progress.append if progress is not None else (lambda _m: None)),
        should_stop=should_stop,
    )


def _inside(rect, point) -> bool:
    x0, y0, w, h = rect
    x, y = point
    return x0 <= x <= x0 + w and y0 <= y <= y0 + h


# ---------- 用例 ----------


def test_probe_finds_fold_and_slider():
    board = ToolbarBoard()
    progress: list[str] = []
    result = _run(board, progress=progress)
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    assert result.slider == {"x": 100, "y": 530, "w": 200, "h": 12}
    # 面板最终应回到展开态（探测会点两次折叠钮以验证可逆）。
    assert board.panel_open
    assert "已识别 折叠按钮" in progress
    assert "已识别 滑杆轨道" in progress


def test_probe_finds_undo_by_ink_drop():
    board = ToolbarBoard()
    result = _run(board)
    assert result.undo is not None
    assert _inside(ToolbarBoard.UNDO, result.undo)
    # 判定依赖真实触发了一次撤销（墨量下降）。
    assert board.undos >= 1


def test_probe_finds_eraser_by_line_erase():
    board = ToolbarBoard()
    result = _run(board)
    assert result.eraser is not None
    assert _inside(ToolbarBoard.ERASER, result.eraser)
    # 压线重描后线白化，说明该候选切到了橡皮。
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)


def test_probe_brush_fingerprints():
    board = ToolbarBoard()
    result = _run(board)
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)
    assert result.brush_selected_lum is not None
    assert result.brush_idle_lum is not None
    assert abs(result.brush_selected_lum - ToolbarBoard.SELECTED_LUM) < 1
    assert abs(result.brush_idle_lum - ToolbarBoard.IDLE_LUM) < 1
    assert result.brush_idle_lum - result.brush_selected_lum >= ToolbarProber.LUM_DELTA_MIN


def test_probe_respects_manual_items():
    board = ToolbarBoard()
    manual = ToolBitmap(
        brush=(30, 515), eraser=(80, 515),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
        undo=(130, 515), fold=(312, 512),
        brush_selected_lum=180.0, brush_idle_lum=250.0,
    )
    result = _run(board, open_bitmap=manual)
    assert result == manual
    assert board.clicks == []  # 全部给定则不点任何候选

    # 仅人工给定滑杆：折叠钮仍应探测。
    board2 = ToolbarBoard()
    partial = ToolBitmap(
        brush=(30, 515), eraser=(80, 515),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
        undo=(130, 515), fold=None,
        brush_selected_lum=180.0, brush_idle_lum=250.0,
    )
    result2 = _run(board2, open_bitmap=partial)
    assert result2.slider == partial.slider
    assert result2.fold is not None
    assert _inside(ToolbarBoard.FOLD, result2.fold)
    assert board2.clicks


def test_probe_clicks_only_outside_canvas():
    board = ToolbarBoard()
    _run(board)
    assert board.clicks
    assert all(y >= CANVAS_BOTTOM for _, y in board.clicks)


def test_probe_stops_on_should_stop():
    board = ToolbarBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, should_stop=lambda: True)
    assert exc.value.code == "E_STOPPED"
