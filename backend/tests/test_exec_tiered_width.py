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
from app.executor.toolbar_probe import ProbeError

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

    def latest_calibration(self, _project_id: str):
        return self.calibration

    def update_task(self, _task_id: str, **fields) -> None:
        self.updates.append(fields)
        if "status" in fields:
            self.status = fields["status"]

    def append_event(self, _task_id: str, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def save_calibration(self, **kwargs) -> None:
        self.saved.append(kwargs)

    def get_task(self, _task_id: str):
        return SimpleNamespace(cursor_path=0)

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
) -> FakeMouse:
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


TABLE_3 = ((0.0, 2.0), (0.5, 4.0), (1.0, 8.0))


def test_buckets_switch_only_when_crossing(monkeypatch) -> None:
    # 目标行程 0.95/0.93 落在同一桶（19），0.90 落到桶 18：只换一次宽。
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
    store = FakeStore(_calibration(tools_open=_tools(slider=None, fold=None)))
    board = FakeBoard(open_=True, tool="eraser")
    params = ExecParams(auto_width=True, width_table=((0.0, 3.0),))

    mouse = _run(store, board, _paths(1), params)

    # 第一次点击必须是把画笔切回来的画笔格点击，且发生在任何拖拽之前。
    assert mouse.clicks[0] == BRUSH_OPEN
    assert board.tool == "brush"
    assert len(mouse.drags) == 1
    assert any("已自动切回画笔" in m for m in store.warnings())

    # 无法纠正：仍点画笔复检后依旧非画笔 → E_NOT_BRUSH，且一笔都不落。
    stuck_store = FakeStore(_calibration(tools_open=_tools(slider=None, fold=None)))
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
    assert stuck_mouse.clicks == [BRUSH_OPEN]


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


def test_resume_recomputes_identical_sequence() -> None:
    store = FakeStore(_calibration(tools_open=_tools()))
    svc = _service(store, FakeMouse(FakeBoard()))
    params = ExecParams(auto_width=True, width_table=TABLE_3, width_tolerance=0.05)
    paths = _paths(5)

    first = svc._build_draw_sequence("t1", paths, params)
    second = svc._build_draw_sequence("t1", paths, params)

    assert first == second  # 续画重算逐位一致
    for cursor in (1, 3):
        assert first[cursor:] == second[cursor:]
    buckets = [bucket for bucket, _edge, _path in first]
    assert buckets == sorted(buckets, reverse=True)
    assert all(
        abs(edge - bucket * params.width_tolerance) < 1e-9
        for bucket, edge, _path in first
    )
    # 真实分桶下每条笔迹只出现一次，续画不会重复/丢失。
    assert len(first) == len(paths)
    assert sorted(map(id, [p for _b, _e, p in first])) == sorted(map(id, paths))


def test_legacy_profile_single_bitmap_fallback(monkeypatch) -> None:
    class FailingProber:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **_kwargs):
            raise ProbeError("E_PROBE_NO_TOOLBAR", "工具条区域未探到任何控件")

    monkeypatch.setattr(service, "ToolbarProber", FailingProber)
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
