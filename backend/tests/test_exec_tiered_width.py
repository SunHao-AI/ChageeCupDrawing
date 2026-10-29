"""执行器逐段笔宽集成测试：容差合批换宽、双态位图、画笔态校验、探测持久化。

用一块假画板模拟「画布 + 工具条」：画布是白色矩形（供 detect_canvas_rect 命中），
工具条上有折叠钮、滑杆轨道、画笔格与橡皮格。面板展开时轨道有深色段，折叠时
没有；画笔格的亮度随当前工具态变化，用来验证亮度指纹门禁。鼠标点击会真正改变
假画板的状态，从而让真实 PanelController / 画笔复检逻辑跑完整闭环。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.geometry import ClientArea
from app.executor import panel_control, pen_order, service
from app.executor.pen_order import width_buckets
from app.executor.service import ExecParams, ExecutorService, TaskError, _RunHandle
from app.executor.toolbar_probe import ProbeError, ToolBitmap

SIZE = (900, 600)  # (h, w)
CANVAS = (150, 200, 450, 650)  # l, t, r, b
TRACK = {"x": 200, "y": 700, "w": 200, "h": 10}
FOLD = {"x": 40, "y": 700}
UNDO = {"x": 160, "y": 760}
BRUSH_OPEN = (80, 700)
ERASER_OPEN = (120, 700)
BRUSH_CLOSED = (60, 790)
SELECTED_LUM = 200
IDLE_LUM = 80

BUCKET_EDGES = [0.95, 0.93, 0.90]


def _canonical_image() -> np.ndarray:
    grid = np.full(SIZE, 220, dtype=np.uint8)
    l, t, r, b = CANVAS
    grid[t:b, l:r] = 255
    return grid


DETECTED = detect_canvas_rect(_canonical_image())
assert DETECTED is not None
RECT_X, RECT_Y, RECT_R, RECT_B = DETECTED
RECT_W, RECT_H = RECT_R - RECT_X, RECT_B - RECT_Y


@pytest.fixture(autouse=True)
def _fast_canvas(monkeypatch):
    """假画板的画布矩形恒定：跳过纯 Python 的全图扫描（真实检测另有单测覆盖）。"""
    monkeypatch.setattr(service, "detect_canvas_rect", lambda _image: DETECTED)
    monkeypatch.setattr(panel_control, "detect_canvas_rect", lambda _image: DETECTED)


class FakeBoard:
    """假画板：面板开合、笔态、画笔指纹都可观测且可被点击改变。"""

    def __init__(
        self,
        *,
        open_: bool = True,
        tool: str = "brush",
        toggle_enabled: bool = True,
        brush_toggle_enabled: bool = True,
        collapsed_brush_visible: bool = False,
    ) -> None:
        self.open = open_
        self.tool = tool
        self.toggle_enabled = toggle_enabled
        self.brush_toggle_enabled = brush_toggle_enabled
        self.clicks: list[tuple[float, float]] = []
        self.drags: list[list[tuple[float, float]]] = []
        # 折叠态下画笔格是否出现在折叠坐标处（模拟两种面板布局）。
        self.collapsed_brush_visible = collapsed_brush_visible

    def image(self) -> np.ndarray:
        grid = np.full(SIZE, 220, dtype=np.uint8)
        l, t, r, b = CANVAS
        grid[t:b, l:r] = 255
        if self.open:
            cy = TRACK["y"] + TRACK["h"] // 2
            grid[cy - 2 : cy + 3, TRACK["x"] : TRACK["x"] + TRACK["w"]] = 0
        lum = SELECTED_LUM if self.tool == "brush" else IDLE_LUM
        cells = [BRUSH_OPEN]
        if self.collapsed_brush_visible:
            cells.append(BRUSH_CLOSED)
        for x, y in cells:
            grid[y - 4 : y + 5, x - 4 : x + 5] = lum
        return grid


class FakeMouse:
    """记录点击/拖拽；点击折叠钮与画笔格会真实改变假画板状态。"""

    def __init__(self, board: FakeBoard) -> None:
        self.board = board
        self.clicks: list[tuple[float, float]] = []
        self.drags: list[list[tuple[float, float]]] = []
        self.ups = 0

    def click(self, x: float, y: float, **_kwargs) -> None:
        self.clicks.append((float(x), float(y)))
        board = self.board
        if board.toggle_enabled and abs(x - FOLD["x"]) <= 20 and abs(y - FOLD["y"]) <= 20:
            board.open = not board.open
            return
        for px, py in (BRUSH_OPEN, BRUSH_CLOSED):
            if abs(x - px) <= 8 and abs(y - py) <= 8 and board.brush_toggle_enabled:
                board.tool = "brush"

    def drag_polyline(self, path, **_kwargs) -> bool:
        self.drags.append(list(path))
        return True

    def up(self) -> None:
        self.ups += 1

    def move_to(self, _x: float, _y: float) -> None:
        pass

    def move_sample(self, _x: float, _y: float) -> None:
        pass


class FakeTarget:
    kind = "win32"

    def __init__(self, board: FakeBoard) -> None:
        self.board = board

    def capture_image(self) -> np.ndarray:
        return self.board.image()

    def capture(self):
        return SimpleNamespace(image=self.board.image())

    def foreground_matches(self) -> bool:
        return True

    def activate(self) -> bool:
        return True

    def alive(self) -> bool:
        return True

    def client_area(self) -> ClientArea:
        return ClientArea(0, 0, SIZE[1], SIZE[0])


class FakeStore:
    """只实现执行器路径用到的存储接口。"""

    def __init__(self, calibration) -> None:
        self.calibration = calibration
        self.events: list[tuple[str, dict]] = []
        self.saved: list[dict] = []
        self.updates: list[dict] = []
        self.status = "ARMING"
        # 续画用例可放入一条带 exec_params 的记录；其余用例保持 None。
        self.task = None

    def latest_calibration(self, _project_id: str):
        return self.calibration

    def update_task(self, _task_id: str, **fields) -> None:
        self.updates.append(fields)
        if "status" in fields:
            self.status = fields["status"]
        if self.task is not None:
            for key, value in fields.items():
                setattr(self.task, key, value)

    def append_event(self, _task_id: str, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def save_calibration(self, **kwargs) -> None:
        self.saved.append(kwargs)

    def get_task(self, _task_id: str):
        return self.task if self.task is not None else SimpleNamespace(cursor_path=0)

    def get_plan(self, _plan_id: str):
        return SimpleNamespace(id="plan1", project_id="p1", paths_path="unused")

    def reopen_task(self, _task_id: str) -> None:
        return None

    def warnings(self) -> list[str]:
        return [
            payload["message"]
            for event_type, payload in self.events
            if event_type == "task.warning"
        ]


def _tools(
    *,
    brush=BRUSH_OPEN,
    eraser=ERASER_OPEN,
    slider=TRACK,
    undo=UNDO,
    fold=FOLD,
    selected_lum=SELECTED_LUM,
    idle_lum=IDLE_LUM,
) -> dict:
    return {
        "brush": brush,
        "eraser": eraser,
        "slider": dict(slider) if slider is not None else None,
        "undo": undo,
        "fold": fold,
        "brush_selected_lum": selected_lum,
        "brush_idle_lum": idle_lum,
    }


def _calibration(*, tools_open=None, tools_collapsed=None, slider=TRACK, fold=FOLD):
    return SimpleNamespace(
        rect_x=RECT_X,
        rect_y=RECT_Y,
        rect_w=RECT_W,
        rect_h=RECT_H,
        slider=dict(slider) if slider is not None else None,
        undo=dict(UNDO),
        fold=dict(fold) if fold is not None else None,
        tools_open=tools_open,
        tools_collapsed=tools_collapsed,
        blank_baseline=None,
        hwnd=1,
        window_title="fake",
        capture_method="win32",
        dpi_scale=1.0,
        source="manual",
        confidence=1.0,
        device_kind="win32",
        device_serial=None,
    )


def _paths(count: int = 3) -> list[list[tuple[float, float]]]:
    return [
        [(20.0 + i * 30.0, 30.0), (80.0 + i * 40.0, 120.0)]
        for i in range(count)
    ]


def _service(store: FakeStore, mouse: FakeMouse) -> ExecutorService:
    return ExecutorService(
        store=store,
        device=None,  # type: ignore[arg-type]
        mouse_factory=lambda: mouse,
        arm_stable_s=0.0,
    )


def _run(
    store: FakeStore,
    board: FakeBoard,
    paths,
    params: ExecParams,
    *,
    cursor: int = 0,
    mouse: FakeMouse | None = None,
) -> FakeMouse:
    if mouse is None:
        mouse = FakeMouse(board)
    svc = _service(store, mouse)
    handle = _RunHandle()
    svc._arm_and_draw(
        "t1",
        SimpleNamespace(id="plan1", project_id="p1"),
        paths,
        params,
        mouse,
        FakeTarget(board),
        0,
        0.02,
        cursor,
        len(paths),
        time.monotonic(),
        handle,
    )
    return mouse


def _slider_clicks(mouse: FakeMouse) -> list[tuple[float, float]]:
    """只取落在滑杆轨道上的点击（折叠钮点击同样会被记录）。"""
    return [
        (x, y)
        for x, y in mouse.clicks
        if TRACK["x"] <= x <= TRACK["x"] + TRACK["w"]
        and TRACK["y"] <= y <= TRACK["y"] + TRACK["h"]
    ]


# 跨态画布用例：折叠态画布盖住滑杆（y=520），展开后画布缩短、滑杆露出。
SLIDER_LOW = {"x": 200, "y": 520, "w": 200, "h": 10}
CANVAS_FOLDED = (0, 0, 400, 560)
CANVAS_EXPANDED = (0, 0, 400, 430)


class _TrackBoard(FakeBoard):
    """额外在 y=520 画滑杆轨道，用于模拟折叠态画布盖住滑杆的真实布局。"""

    def image(self) -> np.ndarray:
        grid = super().image()
        if self.open:
            cy = SLIDER_LOW["y"] + SLIDER_LOW["h"] // 2
            grid[cy - 2 : cy + 3, SLIDER_LOW["x"] : SLIDER_LOW["x"] + SLIDER_LOW["w"]] = 0
        return grid


def test_slider_click_uses_current_frame_canvas(monkeypatch) -> None:
    """N-1：折叠态画布 ⊃ 展开态画布时，换宽仍须点中滑杆并推进档位。

    守卫若沿用展开前（折叠态）的 new_rect，滑杆坐标会被判成落在画布内而拒点，
    active_bucket 不前进，每条笔迹都会重复展开/校验/折叠。
    """
    board = _TrackBoard(open_=False)

    def detect(_image):
        return CANVAS_EXPANDED if board.open else CANVAS_FOLDED

    monkeypatch.setattr(service, "detect_canvas_rect", detect)
    monkeypatch.setattr(panel_control, "detect_canvas_rect", detect)
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    calibration = _calibration(tools_open=_tools(slider=SLIDER_LOW))
    # 校准矩形与折叠态画布一致，避免漂移拦截（本用例只关心守卫矩形）。
    calibration.rect_x, calibration.rect_y = 0, 0
    calibration.rect_w, calibration.rect_h = 400, 560
    store = FakeStore(calibration)
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)

    mouse = _run(store, board, _paths(3), params)

    clicks = [
        (x, y)
        for x, y in mouse.clicks
        if SLIDER_LOW["y"] <= y <= SLIDER_LOW["y"] + SLIDER_LOW["h"]
    ]
    assert len(clicks) == 2  # 两次跨桶各一次，档位已推进
    assert [round(x, 3) for x, _ in clicks] == [
        SLIDER_LOW["x"] + SLIDER_LOW["w"] * 0.95,
        SLIDER_LOW["x"] + SLIDER_LOW["w"] * 0.90,
    ]
    assert all(y == SLIDER_LOW["y"] + SLIDER_LOW["h"] / 2.0 for _, y in clicks)
    assert any("已切换第 20 档笔宽" in m for m in store.warnings())
    assert any("已切换第 19 档笔宽" in m for m in store.warnings())
    assert not any("滑杆坐标落在画布内" in m for m in store.warnings())


TABLE_3 = ((0.0, 2.0), (0.5, 4.0), (1.0, 8.0))


def test_fold_changes_canvas_rect_stops_before_drawing(monkeypatch) -> None:
    """C1：收起面板后画布变大（盖住滑杆区域）时必须停止，不能用旧矩形落笔。

    映射基准若仍是开局（展开态）矩形，整幅笔迹都会整体错位；安全优先即停。
    """
    board = _TrackBoard(open_=True)

    def detect(_image):
        return CANVAS_EXPANDED if board.open else CANVAS_FOLDED

    monkeypatch.setattr(service, "detect_canvas_rect", detect)
    monkeypatch.setattr(panel_control, "detect_canvas_rect", detect)
    calibration = _calibration(tools_open=_tools(slider=SLIDER_LOW))
    # 开局（展开态）画布与校准一致，只让「收起后」的画布发生漂移。
    calibration.rect_x, calibration.rect_y = 0, 0
    calibration.rect_w, calibration.rect_h = 400, 430
    store = FakeStore(calibration)
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)
    mouse = FakeMouse(board)

    with pytest.raises(TaskError) as excinfo:
        _run(store, board, _paths(1), params, mouse=mouse)

    assert excinfo.value.code == "E_CANVAS_DRIFT"
    assert "工具面板开合改变了画布矩形" in excinfo.value.message
    assert "收起" in excinfo.value.message
    assert mouse.drags == []  # 未落下任何笔迹
    assert board.open is False  # 面板已收起，等用户重新校准


def test_fold_failure_after_width_switch_rechecks_mapping(monkeypatch) -> None:
    """C1 残留：换宽后折叠失败且画布已变化，必须停止而非按收起基准继续。"""
    board = _TrackBoard(open_=False)

    def detect(_image):
        return CANVAS_EXPANDED if board.open else CANVAS_FOLDED

    monkeypatch.setattr(service, "detect_canvas_rect", detect)
    monkeypatch.setattr(panel_control, "detect_canvas_rect", detect)
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    real_fold = panel_control.PanelController.fold
    folds = {"n": 0}

    def fold_then_fail(self, slider_rect, fold_point):
        folds["n"] += 1
        if folds["n"] == 1:  # 开局面板已收起：首次 fold 是无需点击的空操作
            return real_fold(self, slider_rect, fold_point)
        return False  # 换宽后收起失败：面板实际仍展开

    monkeypatch.setattr(panel_control.PanelController, "fold", fold_then_fail)
    calibration = _calibration(tools_open=_tools(slider=SLIDER_LOW))
    calibration.rect_x, calibration.rect_y = 0, 0
    calibration.rect_w, calibration.rect_h = 400, 560
    store = FakeStore(calibration)
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)

    with pytest.raises(TaskError) as excinfo:
        _run(store, board, _paths(2), params)

    assert excinfo.value.code == "E_CANVAS_DRIFT"
    assert "工具面板未能收起且画布矩形已变化" in excinfo.value.message
    assert board.open is True  # 折叠确实没生效，画布与收起基准已不一致


def test_calibration_stop_reports_cursor_and_total(monkeypatch) -> None:
    """I5：标定被停止时进度须带真实的 cursor/total，而不是 0/0。"""

    class StoppingCalibrator:
        TEST_MARGIN = 6
        TEST_LINE_LEN = 46
        MIN_SPREAD_RATIO = 0.18

        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **_kwargs):
            raise service.CalibrationError("E_STOPPED", "标定被停止")

    monkeypatch.setattr(service, "PenCalibrator", StoppingCalibrator)
    store = FakeStore(_calibration(tools_open=_tools()))
    board = FakeBoard(open_=True)
    svc = _service(store, FakeMouse(board))

    result = svc._run_pen_calibration(
        "t-stop",
        ExecParams(auto_width=True),
        FakeMouse(board),
        FakeTarget(board),
        None,
        (SIZE[1], SIZE[0]),
        service.Rect(150, 200, 300, 450),
        dict(TRACK),
        dict(UNDO),
        _RunHandle(),
        cursor=2,
        total=7,
    )

    assert result is None
    done = [payload for kind, payload in store.events if kind == "task.done"]
    assert done[-1]["status"] == "STOPPED"
    assert done[-1]["donePaths"] == 2
    assert done[-1]["totalPaths"] == 7


def test_buckets_switch_only_when_crossing(monkeypatch) -> None:
    # 0.95 → 桶 19（第 20 档）；0.93 与 0.90 同落桶 18（第 19 档）：只换一次宽。
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    store = FakeStore(_calibration(tools_open=_tools(brush=None)))
    board = FakeBoard(open_=True)
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)

    mouse = _run(store, board, _paths(3), params)

    clicks = _slider_clicks(mouse)
    assert len(clicks) == 2  # 点击次数恰等于跨桶次数
    assert [round(x, 3) for x, _ in clicks] == [
        TRACK["x"] + TRACK["w"] * 0.95,
        TRACK["x"] + TRACK["w"] * 0.90,
    ]
    assert all(y == TRACK["y"] + TRACK["h"] / 2.0 for _, y in clicks)
    assert len(mouse.drags) == 3
    assert store.status == "DONE"
    assert any("已切换第 20 档笔宽（目标行程 0.95）" in m for m in store.warnings())
    assert any("已切换第 19 档笔宽（目标行程 0.90）" in m for m in store.warnings())


def test_brush_state_enforced_before_first_stroke() -> None:
    # 位图需带折叠钮/滑杆，面板状态才可判定；否则按 B-1 规则应跳过校验。
    store = FakeStore(_calibration(tools_open=_tools()))
    board = FakeBoard(open_=True, tool="eraser")
    params = ExecParams(auto_width=True, width_table=((0.0, 3.0),))

    mouse = _run(store, board, _paths(1), params)

    # 必须发生过画笔格点击（把画笔切回来），且发生在任何拖拽之前。
    assert BRUSH_OPEN in mouse.clicks
    assert board.tool == "brush"
    assert len(mouse.drags) == 1
    assert any("已自动切回画笔" in m for m in store.warnings())

    # 无法纠正：仍点画笔复检后依旧非画笔 → E_NOT_BRUSH，且一笔都不落；
    # 抛错前面板须已收起（最后一击是折叠钮）。
    stuck_store = FakeStore(_calibration(tools_open=_tools()))
    stuck_board = FakeBoard(open_=True, tool="eraser", brush_toggle_enabled=False)
    stuck_mouse = FakeMouse(stuck_board)
    svc = _service(stuck_store, stuck_mouse)
    with pytest.raises(TaskError) as excinfo:
        svc._arm_and_draw(
            "t2",
            SimpleNamespace(id="plan1", project_id="p1"),
            _paths(1),
            params,
            stuck_mouse,
            FakeTarget(stuck_board),
            0,
            0.02,
            0,
            1,
            time.monotonic(),
            _RunHandle(),
        )
    assert excinfo.value.code == "E_NOT_BRUSH"
    assert stuck_mouse.drags == []
    assert BRUSH_OPEN in stuck_mouse.clicks
    assert stuck_mouse.clicks[-1] == (FOLD["x"], FOLD["y"])


def test_unknown_panel_state_skips_brush_guard() -> None:
    """B-1：面板态未知（无折叠按钮）时不得用展开态坐标盲点，只警告并跳过。"""
    store = FakeStore(_calibration(tools_open=_tools(fold=None), fold=None))
    board = FakeBoard(open_=True, tool="eraser")
    params = ExecParams(auto_width=True, width_table=((0.0, 3.0),))

    mouse = _run(store, board, _paths(1), params)

    assert BRUSH_OPEN not in mouse.clicks
    assert board.tool == "eraser"
    assert len(mouse.drags) == 1
    assert any("折叠态无画笔位图，本次跳过画笔校验" in m for m in store.warnings())


def test_unfold_failure_skips_brush_guard() -> None:
    """B-1：折叠态无折叠位图且展开失败时，不点击任何画笔格。"""
    store = FakeStore(_calibration(tools_open=_tools()))
    board = FakeBoard(open_=False, tool="eraser", toggle_enabled=False)
    params = ExecParams(auto_width=True, width_table=((0.0, 3.0),))

    mouse = _run(store, board, _paths(1), params)

    assert BRUSH_OPEN not in mouse.clicks
    assert board.tool == "eraser"
    assert len(mouse.drags) == 1
    assert any("折叠态无画笔位图，本次跳过画笔校验" in m for m in store.warnings())


def test_never_clicks_eraser_during_draw(monkeypatch) -> None:
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    store = FakeStore(_calibration(tools_open=_tools()))
    board = FakeBoard(open_=True, tool="brush")
    params = ExecParams(auto_width=True, width_table=TABLE_3)

    mouse = _run(store, board, _paths(3), params)

    assert _slider_clicks(mouse)  # 确实发生过换宽点击
    for x, y in mouse.clicks:
        assert not (abs(x - ERASER_OPEN[0]) <= 20 and abs(y - ERASER_OPEN[1]) <= 20), (
            f"点击 ({x}, {y}) 落在橡皮热区内"
        )
    assert len(mouse.drags) == 3


def test_uses_collapsed_bitmap_when_panel_folded() -> None:
    store = FakeStore(
        _calibration(
            tools_open=_tools(),
            tools_collapsed=_tools(
                brush=BRUSH_CLOSED, eraser=ERASER_OPEN, slider=None, fold=FOLD
            ),
        )
    )
    board = FakeBoard(open_=False, tool="eraser", collapsed_brush_visible=True)
    params = ExecParams(auto_width=True, width_table=((0.0, 3.0),))

    mouse = _run(store, board, _paths(2), params)

    # 面板折叠时按折叠态位图判定，画笔点击必须落在折叠坐标而不是展开坐标。
    assert mouse.clicks == [BRUSH_CLOSED]
    assert board.tool == "brush"
    assert len(mouse.drags) == 2


def test_missing_collapsed_bitmap_triggers_rescan(monkeypatch) -> None:
    """N-4：复制的校准只有 tools_open、缺 tools_collapsed 且面板折叠时，按需补学展开钮。

    展开钮缺失会导致「面板无法展开→标定跳过→单细笔+跳过画笔校验」；补学后应
    展开、标定、换宽全链路恢复正常，并把收起态位图（含展开钮）持久化。
    """
    calls: list[dict] = []

    class RescanProber:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **kwargs) -> ToolBitmap:
            calls.append(kwargs)
            return ToolBitmap(
                brush=BRUSH_OPEN,
                eraser=ERASER_OPEN,
                slider=dict(TRACK),
                undo=(UNDO["x"], UNDO["y"]),
                fold=(FOLD["x"], FOLD["y"]),
                brush_selected_lum=SELECTED_LUM,
                brush_idle_lum=IDLE_LUM,
                collapsed=ToolBitmap(
                    brush=BRUSH_CLOSED,
                    eraser=ERASER_OPEN,
                    slider=None,
                    undo=(UNDO["x"], UNDO["y"]),
                    fold=(FOLD["x"], FOLD["y"]),
                    brush_selected_lum=SELECTED_LUM,
                    brush_idle_lum=IDLE_LUM,
                ),
            )

    monkeypatch.setattr(service, "ToolbarProber", RescanProber)
    monkeypatch.setattr(service, "PenCalibrator", _FakeCalibrator)
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    _CALIBRATOR_INSTANCES.clear()
    calibration = _calibration(tools_open=_tools())
    store = FakeStore(calibration)
    board = FakeBoard(open_=False)
    params = ExecParams(auto_width=True, width_table=None, width_tolerance=0.05)

    mouse = _run(store, board, _paths(3), params)

    # 触发了收起态重扫，且把已知展开态位图整体作为兜底传入。
    assert calls and calls[-1].get("rescan_collapsed") is True
    assert calls[-1]["open_bitmap"].fold == pytest.approx((FOLD["x"], FOLD["y"]))
    # 收起态位图（含展开钮）被持久化，下次读取即获得展开能力。
    saved = store.saved[-1]
    assert saved["tools_collapsed"] is not None
    assert saved["tools_collapsed"]["fold"] == pytest.approx((FOLD["x"], FOLD["y"]))
    # 补学展开钮后标定与换宽恢复：不再「面板无法展开→单细笔」。
    assert _CALIBRATOR_INSTANCES
    assert _slider_clicks(mouse)
    assert store.status == "DONE"


class _FakeProber:
    """探测成功并返回本次截图帧（new_rect 坐标系）下的工具位图。"""

    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, **_kwargs) -> ToolBitmap:
        return ToolBitmap(
            brush=BRUSH_OPEN,
            eraser=ERASER_OPEN,
            slider=dict(TRACK),
            undo=(UNDO["x"], UNDO["y"]),
            fold=(FOLD["x"], FOLD["y"]),
            brush_selected_lum=SELECTED_LUM,
            brush_idle_lum=IDLE_LUM,
        )


_CALIBRATOR_INSTANCES: list = []


class _FakeCalibrator:
    """模拟标定：画 1 条测试线并返回 3 档宽度表。

    假画板不会因标定落墨改变像素（真实 PenCalibrator 的测宽会失败），这里用
    drag_polyline 记下「那一条测试线」，供用例断言标定确实发生在探测之后。
    """

    # 仅为让 service 的刻度带计算不依赖真实标定器（它读取这三个类属性）。
    TEST_MARGIN = 6
    TEST_LINE_LEN = 46
    MIN_SPREAD_RATIO = 0.18

    def __init__(self, *, mouse, **_kwargs) -> None:
        self._mouse = mouse
        self.slider_track: dict | None = None
        _CALIBRATOR_INSTANCES.append(self)

    def run(
        self,
        *,
        slider_track: dict,
        canvas: tuple[int, int, int, int],
        **_kwargs,
    ) -> SimpleNamespace:
        self.slider_track = dict(slider_track)
        self._mouse.drag_polyline(
            [(canvas[0] + 6, canvas[1] + 10), (canvas[0] + 52, canvas[1] + 10)]
        )
        return SimpleNamespace(
            table=SimpleNamespace(tiers=TABLE_3),
            width_spread=0.5,
        )


def test_probed_bitmap_persisted_in_saved_rect_frame(monkeypatch) -> None:
    """B-3：探测坐标来自 new_rect 帧，写入 profile 前须减回旧 rect 帧。

    构造一个 2/1px 的画布漂移：若持久化未反平移，下次读取会再平移一次，
    滑杆点击位置就会偏移同样距离。
    """
    dx, dy = 2, 1
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    monkeypatch.setattr(service, "ToolbarProber", _FakeProber)
    calibration = _calibration()
    calibration.rect_x = RECT_X - dx
    calibration.rect_y = RECT_Y - dy
    store = FakeStore(calibration)
    board = FakeBoard(open_=True)
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)

    _run(store, board, _paths(3), params)

    saved = store.saved[-1]
    assert saved["source"] == "probe"
    assert saved["rect_x"] == RECT_X - dx
    assert saved["rect_y"] == RECT_Y - dy
    tools = saved["tools_open"]
    assert tools["slider"]["x"] == pytest.approx(TRACK["x"] - dx)
    assert tools["slider"]["y"] == pytest.approx(TRACK["y"] - dy)
    assert tools["brush"] == pytest.approx((BRUSH_OPEN[0] - dx, BRUSH_OPEN[1] - dy))
    assert tools["fold"] == pytest.approx((FOLD["x"] - dx, FOLD["y"] - dy))

    # 用刚写入的 profile 再跑一次：换宽点击不应被再平移一次。
    store2 = FakeStore(SimpleNamespace(**saved))
    mouse2 = _run(store2, FakeBoard(open_=True), _paths(3), params)
    clicks = _slider_clicks(mouse2)
    assert clicks
    assert clicks[0][0] == pytest.approx(TRACK["x"] + TRACK["w"] * 0.95)
    assert clicks[0][1] == pytest.approx(TRACK["y"] + TRACK["h"] / 2.0)


def test_legacy_tier_resume_migrates_once(monkeypatch) -> None:
    """N-3：旧 widthTiers 记录续画时重置一次并迁移参数，第二次不再重复 warning。"""
    store = FakeStore(_calibration(tools_open=_tools()))
    board = FakeBoard(open_=True)
    mouse = FakeMouse(board)
    svc = _service(store, mouse)
    store.task = SimpleNamespace(
        id="t1",
        plan_id="plan1",
        status="STOPPED",
        cursor_path=2,
        exec_params={
            "autoWidth": True,
            "widthTiers": 5,
            "widthTable": [[0.0, 2.0], [0.5, 4.0], [1.0, 8.0]],
        },
    )
    monkeypatch.setattr(svc, "_load_paths", lambda _plan: _paths(3))
    monkeypatch.setattr(svc, "_target", lambda _plan: FakeTarget(board))
    monkeypatch.setattr(svc, "_claim", lambda _target: (_RunHandle(), mouse))
    monkeypatch.setattr(svc, "_spawn", lambda *args, **kwargs: None)

    def legacy_warnings() -> list[str]:
        return [m for m in store.warnings() if "旧版按档位顺序" in m]

    svc.resume(task_id="t1", countdown_ms=0, allow_drift=0.02)

    assert len(legacy_warnings()) == 1
    assert store.task.cursor_path == 0
    assert "widthTiers" not in store.task.exec_params
    assert store.task.exec_params["widthTolerance"] == pytest.approx(0.05)
    assert store.task.exec_params["widthTable"] == [[0.0, 2.0], [0.5, 4.0], [1.0, 8.0]]

    store.task.status = "STOPPED"
    svc.resume(task_id="t1", countdown_ms=0, allow_drift=0.02)

    assert len(legacy_warnings()) == 1  # 迁移后不再重复重置


def test_resume_recomputes_identical_sequence(monkeypatch) -> None:
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    store = FakeStore(_calibration(tools_open=_tools()))
    svc = _service(store, FakeMouse(FakeBoard()))
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)
    paths = _paths(3)

    flat = svc._build_draw_sequence(paths, params)
    assert flat == svc._build_draw_sequence(paths, params)  # 续画重算逐位一致
    buckets = [bucket for bucket, _edge, _path in flat]
    assert buckets == sorted(buckets, reverse=True)
    assert all(
        abs(edge - bucket * params.width_tolerance) < 1e-9
        for bucket, edge, _path in flat
    )

    # 真实断点：第二个桶的首个索引。断点前已画过，不能再画；断点后首击须换到
    # 该桶下缘滑杆位置。
    cursor = next(
        index for index, (bucket, _e, _p) in enumerate(flat) if bucket != flat[0][0]
    )
    assert cursor > 0
    board = FakeBoard(open_=True)
    mouse = _run(store, board, paths, params, cursor=cursor)
    clicks = _slider_clicks(mouse)
    assert len(clicks) == 1
    assert clicks[0][0] == pytest.approx(TRACK["x"] + TRACK["w"] * flat[cursor][1])
    assert len(mouse.drags) == len(flat) - cursor
    drawn = [tuple(drag[0]) for drag in mouse.drags]
    assert drawn == [
        (path[0][0] + RECT_X, path[0][1] + RECT_Y)
        for _b, _e, path in flat[cursor:]
    ]
    before = {
        (path[0][0] + RECT_X, path[0][1] + RECT_Y)
        for _b, _e, path in flat[:cursor]
    }
    assert before.isdisjoint(drawn)  # 断点之前的笔迹不再绘制
    assert flat[cursor] == svc._build_draw_sequence(paths, params)[cursor]


def test_legacy_profile_single_bitmap_fallback(monkeypatch) -> None:
    class FailingProber:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **_kwargs):
            raise ProbeError("E_PROBE_NO_TOOLBAR", "工具条区域未探到任何控件")

    monkeypatch.setattr(service, "ToolbarProber", FailingProber)
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    store = FakeStore(_calibration())  # 旧校准：无 tools_open / tools_collapsed
    board = FakeBoard(open_=True)
    params = ExecParams(auto_width=True, width_table=TABLE_3)

    mouse = _run(store, board, _paths(3), params)

    warnings = store.warnings()
    assert any("工具条自动探测失败" in m for m in warnings)
    assert any("旧校准：无法校验画笔态" in m for m in warnings)
    assert store.saved == []  # 探测失败不写库
    assert store.status == "DONE"
    assert len(mouse.drags) == 3
    # 仍用旧单套坐标换宽（滑杆坐标未被探测结果替换）。
    clicks = _slider_clicks(mouse)
    assert len(clicks) == len({b for b, _e, _p in width_buckets(_paths(3), 0.05)})


def test_width_tiers_key_ignored() -> None:
    params = ExecParams.from_dict({"widthTiers": 9})
    assert params.width_tolerance == 0.05
    assert params.auto_width is True

    data = params.as_dict()
    assert "widthTiers" not in data
    assert data["widthTolerance"] == 0.05
    assert data["autoWidth"] is True

    tuned = ExecParams.from_dict({"widthTolerance": 0.1, "widthTiers": 3})
    assert tuned.width_tolerance == 0.1


def test_probe_bootstraps_calibration_without_manual_anchors(monkeypatch) -> None:
    """全新用户：校准只有画布矩形，探测须先跑，标定才有滑杆坐标可用。"""
    monkeypatch.setattr(service, "ToolbarProber", _FakeProber)
    monkeypatch.setattr(service, "PenCalibrator", _FakeCalibrator)
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    _CALIBRATOR_INSTANCES.clear()
    calibration = _calibration(tools_open=None, slider=None, fold=None)
    store = FakeStore(calibration)
    params = ExecParams(auto_width=True, width_table=None, width_tolerance=0.05)

    mouse = _run(store, FakeBoard(open_=True), _paths(3), params)

    # 探测成功即持久化成一条新校准记录，滑杆坐标落在假画板的轨道内。
    assert len(store.saved) == 1
    assert store.saved[0]["source"] == "probe"
    slider = store.saved[0]["tools_open"]["slider"]
    assert TRACK["x"] <= slider["x"] <= TRACK["x"] + TRACK["w"]
    assert store.status == "DONE"

    # 标定器拿到的滑杆矩形就是探测结果（TRACK），而非旧校准的空坐标。
    assert _CALIBRATOR_INSTANCES
    assert _CALIBRATOR_INSTANCES[-1].slider_track == TRACK

    # 3 条用户路径 + _FakeCalibrator 模拟的 1 条标定测试线（真实标定会画 5 条，
    # 但假画板无法测宽，故用最小假标定只记一条）。
    assert len(mouse.drags) == 3 + 1
    assert _slider_clicks(mouse)  # 标定与换宽都会点滑杆
    assert not any("未标定粗细滑杆" in m for m in store.warnings())


def test_probe_runs_before_brush_guard_and_calibration(monkeypatch) -> None:
    """用户裁决的顺序：探测 → 画笔校验 → 笔宽标定。"""
    events: list[str] = []

    class EventProber(_FakeProber):
        def run(self, **kwargs) -> ToolBitmap:
            events.append("probe")
            return super().run(**kwargs)

    class EventMouse(FakeMouse):
        def click(self, x: float, y: float, **kwargs) -> None:
            if (
                TRACK["x"] <= x <= TRACK["x"] + TRACK["w"]
                and TRACK["y"] <= y <= TRACK["y"] + TRACK["h"]
            ):
                events.append("slider-click")
            elif any(
                abs(x - px) <= 8 and abs(y - py) <= 8
                for px, py in (BRUSH_OPEN, BRUSH_CLOSED)
            ):
                events.append("brush-click")
            elif abs(x - FOLD["x"]) <= 20 and abs(y - FOLD["y"]) <= 20:
                events.append("fold-click")
            super().click(x, y, **kwargs)

    class EventCalibrator(_FakeCalibrator):
        def run(self, **kwargs) -> SimpleNamespace:
            events.append("calibrate")
            return super().run(**kwargs)

    monkeypatch.setattr(service, "ToolbarProber", EventProber)
    monkeypatch.setattr(service, "PenCalibrator", EventCalibrator)
    # 保证绘制期跨桶换宽，从而产生「标定之后」的滑杆点击。
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: BUCKET_EDGES)
    calibration = _calibration(tools_open=None, slider=None, fold=None)
    store = FakeStore(calibration)
    params = ExecParams(auto_width=True, width_table=None, width_tolerance=0.05)
    board = FakeBoard(open_=True, tool="eraser")

    _run(store, board, _paths(3), params, mouse=EventMouse(board))

    # 探测 → 画笔校验 → 标定 → 绘制期换宽。滑杆点击来自标定之后的换宽路径
    # （假标定内部不点滑杆），因此可把 calibrate 与 slider-click 分开锁定。
    assert events.index("probe") < events.index("brush-click")
    assert events.index("brush-click") < events.index("calibrate")
    assert events.index("calibrate") < events.index("slider-click")


class _InkedBoard(FakeBoard):
    """画布左上角已有一块用户墨迹（占比 > PROBE_BLANK_MAX）。"""

    def image(self) -> np.ndarray:
        grid = super().image()
        l, t, _r, _b = CANVAS
        grid[t + 20 : t + 80, l + 20 : l + 80] = 0
        return grid


def test_probe_skipped_when_canvas_not_blank(monkeypatch) -> None:
    """C2：画布非空白时绝不盲点工具条候选（可能点中清空类控件）。"""
    prober_calls: list[object] = []

    class RecordingProber(_FakeProber):
        def __init__(self, **kwargs) -> None:
            prober_calls.append(self)
            super().__init__(**kwargs)

    monkeypatch.setattr(service, "ToolbarProber", RecordingProber)
    calibration = _calibration(tools_open=None, slider=None, fold=None)
    store = FakeStore(calibration)
    board = _InkedBoard(open_=True)
    params = ExecParams(auto_width=True, width_table=None, width_tolerance=0.05)

    mouse = _run(store, board, _paths(1), params)

    assert prober_calls == []  # 探测根本未启动
    assert store.saved == []  # 没有写入任何探测结果
    assert any("画布上已有笔迹，已跳过工具条自动探测" in m for m in store.warnings())
    # 沿用既有降级：无滑杆 → 单细笔绘制，仍完成任务。
    assert any("未标定粗细滑杆" in m for m in store.warnings())
    assert len(mouse.drags) == 1
    assert store.status == "DONE"


def _warning_codes(store: FakeStore) -> list[str | None]:
    return [
        payload.get("code")
        for event_type, payload in store.events
        if event_type == "task.warning"
    ]


def test_probe_runs_without_auto_width(monkeypatch) -> None:
    """§6.1：探测与自动变粗解耦——单细笔模式首画且无双态位图也探测。

    撤销/折叠/画笔坐标的价值独立于笔宽；探测成功即持久化，且单细笔模式
    没有后续 panel 收起流程，探测后必须就地折回面板，否则画布下部一直被遮。
    """
    monkeypatch.setattr(service, "ToolbarProber", _FakeProber)
    _CALIBRATOR_INSTANCES.clear()
    monkeypatch.setattr(service, "PenCalibrator", _FakeCalibrator)
    store = FakeStore(_calibration(tools_open=None, slider=None, fold=None))
    board = FakeBoard(open_=True)
    params = ExecParams(auto_width=False)

    mouse = _run(store, board, _paths(2), params)

    saved = store.saved[-1]
    assert saved["source"] == "probe"
    assert saved["tools_open"]["undo"] == pytest.approx((UNDO["x"], UNDO["y"]))
    assert _CALIBRATOR_INSTANCES == []  # 单细笔模式绝不触发标定
    assert _slider_clicks(mouse) == []  # 没有换宽点击
    assert board.open is False  # 探测展开的面板已就地折回
    assert len(mouse.drags) == 2
    assert store.status == "DONE"


def test_probe_skipped_dirty_warning_carries_code(monkeypatch) -> None:
    """§7.2：脏画布跳过探测的 warning 必须带结构化 code，供前端渲染引导卡。"""
    prober_calls: list[object] = []

    class RecordingProber(_FakeProber):
        def __init__(self, **kwargs) -> None:
            prober_calls.append(self)
            super().__init__(**kwargs)

    monkeypatch.setattr(service, "ToolbarProber", RecordingProber)
    store = FakeStore(_calibration(tools_open=None, slider=None, fold=None))
    params = ExecParams(auto_width=False)

    mouse = _run(store, _InkedBoard(open_=True), _paths(1), params)

    assert prober_calls == []
    assert "probe_skipped_dirty" in _warning_codes(store)
    assert len(mouse.drags) == 1
    assert store.status == "DONE"


def test_probe_failure_warning_carries_code(monkeypatch) -> None:
    """§7.2：探测失败的 warning 带 probe_failed 码；失败只降级不阻断绘制。"""

    class FailingProber:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **_kwargs) -> ToolBitmap:
            raise ProbeError("E_NO_TOOLBAR", "画布下方未找到工具条")

    monkeypatch.setattr(service, "ToolbarProber", FailingProber)
    store = FakeStore(_calibration(tools_open=None, slider=None, fold=None))
    params = ExecParams(auto_width=False)

    mouse = _run(store, FakeBoard(open_=True), _paths(1), params)

    assert store.saved == []  # 失败不落探测记录
    assert "probe_failed" in _warning_codes(store)
    assert len(mouse.drags) == 1
    assert store.status == "DONE"


def test_canvas_blank_gate_uses_pixels_not_fraction() -> None:
    """C2 残留：46×3 的探测残留测试线（≈138 px）不得被判成空白。"""

    class _ImageTarget(FakeTarget):
        def __init__(self, image: np.ndarray) -> None:
            self._image = image

        def capture_image(self) -> np.ndarray:
            return self._image

    svc = _service(FakeStore(_calibration()), FakeMouse(FakeBoard(open_=True)))
    rect = service.Rect(*CANVAS[:2], CANVAS[2] - CANVAS[0], CANVAS[3] - CANVAS[1])
    calibration = _calibration()
    blank = np.full(SIZE, 255, dtype=np.uint8)
    assert svc._canvas_is_blank(_ImageTarget(blank), rect, calibration) is True

    residue = blank.copy()
    residue[250:253, 250:296] = 0  # 46×3 = 138 px，正是探测测试线的量级
    assert svc._canvas_is_blank(_ImageTarget(residue), rect, calibration) is False
