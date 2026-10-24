"""ToolbarProber 仿真测试：合成工具栏按 panel_open/tool 状态渲染，点击按控件矩形派发状态迁移。

画板只模拟行为（面板展开/折叠、工具切换、撤销、滑杆），不模拟图标内容，
与 ToolbarProber「点击→观测变化」的探测判据一一对应。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.executor.ink_measure import ink_occupied_fraction
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
        self.pending_touched = False
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
        if not np.array_equal(prev, region):
            self.pending_touched = True

    def undo(self) -> None:
        self.undos += 1
        if not self.strokes:
            return
        for ys0, ys1, xs0, xs1, prev in reversed(self.strokes.pop()):
            self.canvas_pixels[ys0:ys1, xs0:xs1] = prev

    def band_ink(self) -> float:
        return float((self.canvas_pixels[70:110, 46:246] < 160).mean())


class FoldInterferenceBoard(ToolbarBoard):
    """(42,512) 放一个只能把面板永久关掉的干扰控件；真实折叠钮仍在 FOLD 矩形。"""

    INTERFERENCE = (42, 512)

    def __init__(self) -> None:
        super().__init__()
        self.interference_hits = 0

    def handle_click(self, x: float, y: float) -> None:
        if (
            abs(x - self.INTERFERENCE[0]) <= 2
            and abs(y - self.INTERFERENCE[1]) <= 2
            and self.interference_hits < 2
        ):
            self.interference_hits += 1
            self.panel_open = False
            return
        super().handle_click(x, y)


class NoUndoBoard(ToolbarBoard):
    """撤销格被完全屏蔽：探测既找不到撤销，也无法清理测试笔迹。"""

    def handle_click(self, x: float, y: float) -> None:
        if self._hit(self._tool_rect(self.UNDO), x, y):
            return
        super().handle_click(x, y)


class SuppressingBoard(ToolbarBoard):
    """(42,512) 点击会擦掉刻度带右半并切到橡皮，用于验证撤销探测的补描恢复。"""

    SUPPRESS = (42, 512)

    def handle_click(self, x: float, y: float) -> None:
        if abs(x - self.SUPPRESS[0]) <= 2 and abs(y - self.SUPPRESS[1]) <= 2:
            self.tool = "eraser"
            self.canvas_pixels[70:110, 146:246] = 255
            return
        super().handle_click(x, y)


class EmptyBoard(ToolbarBoard):
    """完全没有工具条：无滑杆、无折叠钮。"""

    def image(self) -> np.ndarray:
        return np.full((700, 600), 255, dtype=np.uint8)

    def handle_click(self, x: float, y: float) -> None:
        return


class NoSliderBoard(ToolbarBoard):
    """有折叠钮与工具格，但任何状态下都不画滑杆。"""

    def image(self) -> np.ndarray:
        img = super().image()
        x, y, w, h = self.SLIDER
        img[y : y + h, x : x + w] = 255
        return img


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
        self.board.pending_touched = False
        self.press_pos = self.pos
        self.down_flag = True

    def up(self):
        if self.board.pending and self.board.pending_touched:
            self.board.strokes.append(self.board.pending)
            self.board.pending = []
        elif not self.board.pending:
            # 无位移的按下-抬起才算一次「点击」；带位移的按下属于描线。
            self.board.clicks.append(self.press_pos)
        self.board.pending = []
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


def _prober(board: ToolbarBoard) -> tuple[ToolbarProber, ToolbarMouse]:
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


def _manual(**overrides) -> ToolBitmap:
    fields = dict(
        brush=None, eraser=None, slider=None, undo=None, fold=None,
        brush_selected_lum=None, brush_idle_lum=None,
    )
    fields.update(overrides)
    return ToolBitmap(**fields)


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

    # 仅人工给定滑杆：折叠钮仍应探测，且人工控件邻域候选不应被点。
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
    # 人工画笔/橡皮/撤销/滑杆全部进入折叠阶段跳过集合。
    assert (14.0, 512.0) not in board2.clicks
    assert (42.0, 512.0) not in board2.clicks
    assert (70.0, 512.0) not in board2.clicks
    assert (126.0, 512.0) not in board2.clicks


def test_probe_clicks_only_outside_canvas():
    board = ToolbarBoard()
    _run(board)
    assert board.clicks
    assert all(not _inside(CANVAS, point) for point in board.clicks)
    assert all(point[1] > CANVAS_BOTTOM for point in board.clicks)


def test_probe_skips_known_control_neighborhood():
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    prober._candidates = prober._make_candidates((700, 600))
    skip = prober._skip_points(
        brush=(14, 512),
        eraser=(70, 512),
        undo=(126, 512),
        fold=(322, 512),
        slider={"x": 100, "y": 530, "w": 200, "h": 12},
    )
    # 宽按钮内相邻候选（相距一个网格步长）必须一起跳过。
    assert (42, 512) in skip
    assert (14, 484) in skip
    assert (98, 512) in skip
    assert (350, 512) in skip
    # 滑杆矩形内的候选也跳过。
    assert (126, 540) in skip
    assert (182, 512) not in skip


def test_probe_continues_after_irreversible_fold_candidate():
    board = FoldInterferenceBoard()
    result = _run(board)
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    # 干扰点只做一次翻转 + 一次恢复尝试即放弃（之后不再触发干扰）。
    assert board.interference_hits == 2


def test_probe_cleans_band_and_stroke_history():
    board = ToolbarBoard()
    result = _run(board)
    assert result.brush is not None
    assert result.undo is not None
    assert board.strokes == []
    assert board.band_ink() == 0.0
    assert board.tool == "brush"


def test_probe_keeps_manual_lum_and_only_nulls_computed():
    # 人工 selected 与自动 idle 差不足：只把计算所得 idle 置 None，保留人工值。
    board = ToolbarBoard()
    result = _run(board, open_bitmap=_manual(brush_selected_lum=240.0))
    assert result.brush_selected_lum == 240.0
    assert result.brush_idle_lum is None
    assert result.brush is not None

    # 差足够时保留人工 selected 并使用计算 idle。
    board2 = ToolbarBoard()
    result2 = _run(board2, open_bitmap=_manual(brush_selected_lum=123.0))
    assert result2.brush_selected_lum == 123.0
    assert result2.brush_idle_lum is not None


def test_probe_ends_with_brush_when_lums_manual():
    board = ToolbarBoard()
    board.tool = "eraser"  # 从橡皮态开始，工具探测会以橡皮扫描收尾。
    result = _run(
        board,
        open_bitmap=_manual(
            undo=(130, 515),
            brush_selected_lum=180.0,
            brush_idle_lum=250.0,
        ),
    )
    assert result.brush is not None
    assert result.eraser is not None
    assert board.tool == "brush"


def test_probe_restores_drawing_tool_before_supplement():
    board = SuppressingBoard()
    result = _run(board)
    assert result.undo is not None
    assert _inside(ToolbarBoard.UNDO, result.undo)


def test_probe_rejects_manual_point_inside_canvas():
    board = ToolbarBoard()
    result = _run(
        board,
        open_bitmap=_manual(fold=(100, 100), brush=(50, 100)),
    )
    # 画布内的人工点被丢弃，改由探测得到真实控件。
    assert result.fold is not None
    assert _inside(ToolbarBoard.FOLD, result.fold)
    assert result.brush is not None
    assert _inside(ToolbarBoard.BRUSH, result.brush)
    assert (100.0, 100.0) not in board.clicks
    assert (50.0, 100.0) not in board.clicks
    assert all(not _inside(CANVAS, point) for point in board.clicks)


def test_probe_click_guard_rejects_canvas_points():
    board = ToolbarBoard()
    prober, _ = _prober(board)
    prober._canvas = CANVAS
    with pytest.raises(ProbeError) as exc:
        prober._click(100.0, 100.0)
    assert exc.value.code == "E_PROBE_CLICK_IN_CANVAS"
    assert board.clicks == []


def test_probe_rejects_malformed_manual_slider():
    board = ToolbarBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, open_bitmap=_manual(slider={"x": 100}))
    assert exc.value.code == "E_PROBE_BAD_MANUAL"
    assert board.clicks == []


def test_probe_raises_when_probe_ink_uncleanable():
    board = NoUndoBoard()
    with pytest.raises(ProbeError) as exc:
        _run(
            board,
            open_bitmap=_manual(
                fold=(312, 512),
                slider={"x": 100, "y": 530, "w": 200, "h": 12},
                brush=(30, 515),
                eraser=(80, 515),
                brush_selected_lum=180.0,
                brush_idle_lum=250.0,
            ),
        )
    # undo 未知且刻度带留有测试笔迹：必须上报，而不是留下无法清理的墨。
    assert exc.value.code == "E_PROBE_UNCLEANED"


def test_probe_raises_no_toolbar():
    board = EmptyBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board)
    assert exc.value.code == "E_PROBE_NO_TOOLBAR"


def test_probe_raises_no_slider():
    board = NoSliderBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, open_bitmap=_manual(fold=(312, 512)))
    assert exc.value.code == "E_PROBE_NO_SLIDER"


def test_probe_stops_on_should_stop():
    board = ToolbarBoard()
    with pytest.raises(ProbeError) as exc:
        _run(board, should_stop=lambda: True)
    assert exc.value.code == "E_STOPPED"


def test_probe_stops_mid_scan():
    board = ToolbarBoard()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(ProbeError) as exc:
        _run(board, should_stop=should_stop)
    assert exc.value.code == "E_STOPPED"
    assert board.clicks  # 已经开始了候选扫描
