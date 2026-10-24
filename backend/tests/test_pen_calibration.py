"""PenCalibrator 仿真测试：假画布模拟「滑杆位置→笔宽」，验证扫描/建表/清空全流程。"""

from __future__ import annotations

import numpy as np

from app.executor.pen_calibration import CalibrationError, CalibrationResult, PenCalibrator

CANVAS = (40, 60, 300, 400)
TRACK = {"x": 100, "y": 520, "w": 200, "h": 12}
UNDO = {"x": 60, "y": 460}


class FakeBoard:
    """画布状态：黑色笔画集合；滑杆位置线性映射笔宽；撤销弹出一笔。"""

    def __init__(self, min_w: float = 3.0, max_w: float = 25.0, undo_ok: bool = True) -> None:
        self.grid = np.full((700, 600), 255, dtype=np.uint8)
        self.stroke_width = min_w
        self.min_w, self.max_w = min_w, max_w
        self.strokes: list[tuple[slice, slice, int]] = []
        self.undo_ok = undo_ok
        self.undos = 0

    def set_width_from_slider(self, frac: float) -> None:
        self.stroke_width = self.min_w + (self.max_w - self.min_w) * frac

    def stroke(self, x0: float, y0: float, x1: float, y1: float) -> None:
        w = max(1, int(round(self.stroke_width)))
        half = w // 2
        xs0, xs1 = int(min(x0, x1)), int(max(x0, x1)) + 1
        ys0, ys1 = int(min(y0, y1)) - half, int(max(y0, y1)) + half + 1
        self.grid[ys0:ys1, xs0:xs1] = 0

    def undo(self) -> None:
        self.undos += 1
        if not self.undo_ok or not self.strokes:
            return
        segments = self.strokes.pop()
        for ys, xs in segments:
            self.grid[ys, xs] = 255


class FakeMouseBase:
    def __init__(self, board: FakeBoard) -> None:
        self.board = board
        self.pos = (0.0, 0.0)
        self.down_flag = False

    def move_to(self, x, y):
        self.pos = (float(x), float(y))

    def move_sample(self, x, y, no_coalesce=True):
        if self.down_flag:
            self.board.stroke(self.pos[0], self.pos[1], float(x), float(y))
        self.pos = (float(x), float(y))

    def down(self):
        self.down_flag = True

    def up(self):
        self.down_flag = False


class FakeProbe:
    def __init__(self, board: FakeBoard, track: dict) -> None:
        self.board = board
        self.track = track
        self.clicks: list[tuple[float, float]] = []

    def capture_image(self):
        return self.board.grid

    def foreground_matches(self) -> bool:
        return True

    def activate(self) -> bool:
        return True

    def handle_click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        if abs(y - (self.track["y"] + self.track["h"] / 2)) < self.track["h"]:
            frac = (x - self.track["x"]) / self.track["w"]
            self.board.set_width_from_slider(max(0.0, min(1.0, frac)))
        elif abs(x - UNDO["x"]) < 20 and abs(y - UNDO["y"]) < 20:
            self.board.undo()


class FakeMouse(FakeMouseBase):
    """点击经 FakeProbe 派发：滑杆点击改宽度，撤销点击弹出一笔。

    真实画板的「一笔」= 一次按下-抬起：move_sample 立即落墨（供截图测宽），
    但撤销时整条折线一起消失。
    """

    def __init__(self, board: FakeBoard, probe: FakeProbe) -> None:
        super().__init__(board)
        self.probe = probe
        self.pending: list[tuple[slice, slice]] = []

    def down(self):
        self.probe.handle_click(*self.pos)
        self.pending = []
        self.down_flag = True

    def move_sample(self, x, y, no_coalesce=True):
        if self.down_flag:
            self.board.stroke(self.pos[0], self.pos[1], float(x), float(y))
            w = max(1, int(round(self.board.stroke_width)))
            half = w // 2
            xs = slice(int(min(self.pos[0], x)), int(max(self.pos[0], x)) + 1)
            ys = slice(int(min(self.pos[1], y)) - half, int(max(self.pos[1], y)) + half + 1)
            self.pending.append((ys, xs))
        self.pos = (float(x), float(y))

    def up(self):
        if self.pending:
            self.board.strokes.append(self.pending)
            self.pending = []
        self.down_flag = False


def _calibrator(board: FakeBoard, probe: FakeProbe, tiers: int):
    mouse = FakeMouse(board, probe)
    calib = PenCalibrator(
        probe=probe, mouse=mouse, to_screen=lambda x, y: (x, y),
        sleep=lambda _s: None,
    )
    return calib, mouse


def _run(board: FakeBoard, tiers: int = 5) -> CalibrationResult:
    probe = FakeProbe(board, TRACK)
    calib, _ = _calibrator(board, probe, tiers)
    return calib.run(
        slider_track=TRACK, undo_point=UNDO, tiers=tiers,
        canvas=CANVAS, on_progress=lambda _m: None, should_stop=lambda: False,
    )


def test_scan_builds_monotonic_table_and_clears() -> None:
    board = FakeBoard()
    result = _run(board, tiers=5)
    assert result.cleared
    assert result.table.count == 5
    widths = [w for _, w in result.table.tiers]
    fracs = [f for f, _ in result.table.tiers]
    assert widths == sorted(widths)
    assert fracs == sorted(fracs)
    # 画布按清空判据（墨占比 ≤ CLEAR_CONFIRM_OCCUPIED）确认已清空。
    from app.executor.pen_calibration import PenCalibrator as PC

    assert (board.grid < 160).mean() <= PC.CLEAR_CONFIRM_OCCUPIED
    # 档位覆盖细到粗：最粗档接近滑杆最大值。
    assert widths[-1] >= 20.0
    assert widths[0] <= 7.0


def test_uniform_pen_width_falls_back() -> None:
    board = FakeBoard(min_w=6.0, max_w=7.0)  # 宽度差 <18% → 视为无级差
    result = _run(board, tiers=5)
    assert result.cleared
    assert result.table.count == 1
    from app.executor.pen_calibration import PenCalibrator as PC

    assert (board.grid < 160).mean() <= PC.CLEAR_CONFIRM_OCCUPIED


def test_missing_undo_raises_not_cleared() -> None:
    board = FakeBoard()
    probe = FakeProbe(board, TRACK)
    calib, _ = _calibrator(board, probe, 5)
    try:
        calib.run(
            slider_track=TRACK, undo_point=None, tiers=5,
            canvas=CANVAS, on_progress=lambda _m: None, should_stop=lambda: False,
        )
        raise AssertionError("应抛 CalibrationError")
    except CalibrationError as exc:
        assert exc.code == "E_CALIBRATION_NOT_CLEARED"


def test_slider_rect_validated() -> None:
    board = FakeBoard()
    probe = FakeProbe(board, TRACK)
    calib, _ = _calibrator(board, probe, 5)
    try:
        calib.run(
            slider_track={"x": 0, "y": 0, "w": 5, "h": 1}, undo_point=UNDO,
            tiers=5, canvas=CANVAS, on_progress=lambda _m: None, should_stop=lambda: False,
        )
        raise AssertionError("应抛 CalibrationError")
    except CalibrationError as exc:
        assert exc.code == "E_SLIDER_RECT"
