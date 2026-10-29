"""一键自动校准（ExecutorService.auto_calibrate）集成测试。

复用 test_toolbar_probe 的行为仿真画板（点击→状态迁移→像素变化），接真实
Store 与 ExecutorService，只把真实鼠标/截图/等待换成假件：验证画布识别、
脏画布门禁、快照落盘、收起态重扫入库、单独识别的人工兜底、事件总线形状、
并发冲突与中止路径。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.device.capture import CaptureResult
from app.device.geometry import ClientArea
from app.executor import service, toolbar_probe
from app.executor.service import ExecutorService, TaskError, step_to_warning
from app.executor.toolbar_probe import ToolbarProber
from app.store.store import Store

from test_toolbar_probe import ToolbarBoard, ToolbarMouse  # noqa: E402

HWND = 1234
# 与仿真板一致的画布「检出」结果（l, t, r, b）：真实 detect_canvas_rect 对
# 全白贴边图返回 None，这里按仿真语义固定返回，检测算法本身另有单测覆盖。
CANVAS_DETECT = (40, 60, 340, 420)
CANVAS = (40, 60, 300, 360)


def _center(rect):
    x, y, w, h = rect
    return (x + w / 2.0, y + h / 2.0)


def _near(rect, point, tol=8):
    cx, cy = _center(rect)
    if isinstance(point, dict):
        point = (point["x"], point["y"])
    half_x, half_y = rect[2] / 2.0, rect[3] / 2.0
    return abs(point[0] - cx) <= half_x + tol and abs(point[1] - cy) <= half_y + tol


class FakeDevice:
    def __init__(self, board: ToolbarBoard) -> None:
        self.board = board

    def alive(self, hwnd: int) -> bool:
        return hwnd == HWND

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        return CaptureResult(image=self.board.image(), method="maa")

    def client_area(self, hwnd: int) -> ClientArea:
        return ClientArea(0, 0, 600, 700)  # 与图像同尺寸 → to_screen 恒等

    def title(self, hwnd: int) -> str:
        return "霸王茶姬"

    def dpi_scale(self, hwnd: int) -> float:
        return 1.0


class FakeProbe:
    def foreground_matches(self, hwnd: int) -> bool:
        return True

    def activate(self, hwnd: int) -> bool:
        return True

    def window_usable(self, hwnd: int) -> bool:
        return True


def _make(tmp_path: Path, board: ToolbarBoard, monkeypatch):
    """真实 Store + 假设备/鼠标/等待的 ExecutorService；sleep 注成 no-op。"""
    store = Store(tmp_path / "data")
    store.init()
    store.create_project(
        project_id="p1", name="t", image_path=Path("x.png"),
        image_width=300, image_height=400,
    )
    mouse = ToolbarMouse(board)
    monkeypatch.setattr(
        service, "detect_canvas_rect", lambda _image, **_kw: CANVAS_DETECT
    )
    monkeypatch.setattr(
        toolbar_probe, "detect_canvas_rect", lambda _image, **_kw: CANVAS_DETECT
    )
    real_prober_cls = ToolbarProber
    monkeypatch.setattr(
        service, "ToolbarProber",
        lambda **kwargs: real_prober_cls(sleep=lambda _s: None, **kwargs),
    )
    svc = ExecutorService(
        store,
        FakeDevice(board),
        mouse_factory=lambda: mouse,
        guard_probe=FakeProbe(),
        arm_stable_s=0.0,
    )
    return store, svc


def _seed_calibration(store: Store, **overrides) -> None:
    fields = dict(
        project_id="p1", hwnd=HWND, window_title="霸王茶姬", capture_method="maa",
        rect_x=CANVAS[0], rect_y=CANVAS[1], rect_w=CANVAS[2], rect_h=CANVAS[3],
        dpi_scale=1.0, source="manual", confidence=1.0,
        slider={"x": ToolbarBoard.SLIDER[0], "y": ToolbarBoard.SLIDER[1],
                "w": ToolbarBoard.SLIDER[2], "h": ToolbarBoard.SLIDER[3]},
        undo={"x": _center(ToolbarBoard.UNDO)[0], "y": _center(ToolbarBoard.UNDO)[1]},
        fold={"x": _center(ToolbarBoard.FOLD)[0], "y": _center(ToolbarBoard.FOLD)[1]},
        tools_open={
            "brush": list(_center(ToolbarBoard.BRUSH)),
            "eraser": list(_center(ToolbarBoard.ERASER)),
            "slider": {"x": ToolbarBoard.SLIDER[0], "y": ToolbarBoard.SLIDER[1],
                       "w": ToolbarBoard.SLIDER[2], "h": ToolbarBoard.SLIDER[3]},
            "undo": list(_center(ToolbarBoard.UNDO)),
            "fold": list(_center(ToolbarBoard.FOLD)),
            "brush_selected_lum": 180.0,
            "brush_idle_lum": 250.0,
        },
        tools_collapsed=None,
    )
    fields.update(overrides)
    store.save_calibration(**fields)


# ---------- 前置门禁 ----------


def test_project_not_found(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("nope", hwnd=HWND)
    assert (exc.value.status, exc.value.code) == (404, "E_NOT_FOUND")


def test_fresh_project_requires_hwnd(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("p1")
    assert (exc.value.status, exc.value.code) == (422, "E_CANVAS_MISSING")


def test_canvas_not_found(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    monkeypatch.setattr(service, "detect_canvas_rect", lambda _image, **_kw: None)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("p1", hwnd=HWND)
    assert (exc.value.status, exc.value.code) == (422, "E_CANVAS_NOT_FOUND")
    # 失败终态也要在事件总线留下可观测的 failed 记录。
    events, running = svc.calibration_events("p1", 0)
    assert running is False
    assert events[-1]["type"] == "calibration.failed"


def test_dirty_canvas_blocks_then_confirms(tmp_path, monkeypatch):
    board = ToolbarBoard()
    # 画布右下角一块墨：远离探测刻度带（画布顶部），只触发行脏画布门禁。
    board.canvas_pixels[330:370, 250:330] = 0
    _store, svc = _make(tmp_path, board, monkeypatch)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("p1", hwnd=HWND)
    assert (exc.value.status, exc.value.code) == (409, "E_CANVAS_DIRTY")
    # 确认授权后放行：能走完探测并落库。
    result = svc.auto_calibrate("p1", hwnd=HWND, confirm_dirty_probe=True)
    assert result["calibrationId"] is not None


# ---------- 全量一键校准 ----------


def test_full_auto_calibrate_persists_snapshot_and_collapsed(tmp_path, monkeypatch):
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)
    result = svc.auto_calibrate("p1", hwnd=HWND)

    assert result["rect"] == {"x": 40, "y": 60, "w": 300, "h": 360}
    assert result["collapsedCaptured"] is True
    snapshot_id = result["snapshotId"]
    assert snapshot_id
    assert store.snapshot_path("p1", snapshot_id).is_file()

    profile = store.latest_calibration("p1")
    assert profile.snapshot_id == snapshot_id
    assert (profile.rect_x, profile.rect_y, profile.rect_w, profile.rect_h) == CANVAS
    assert profile.source == "probe"
    assert board.panel_open  # 探测收尾恢复展开态

    # 展开态：控件落在仿真板对应矩形内，滑杆轨道与板一致。
    open_map = profile.tools_open
    for name, rect in (("undo", ToolbarBoard.UNDO),
                       ("eraser", ToolbarBoard.ERASER),
                       ("fold", ToolbarBoard.FOLD)):
        assert _near(rect, open_map[name]), name
    assert open_map["slider"] == {
        "x": ToolbarBoard.SLIDER[0], "y": ToolbarBoard.SLIDER[1],
        "w": ToolbarBoard.SLIDER[2], "h": ToolbarBoard.SLIDER[3],
    }

    # 收起态：x 与展开态完全一致（行平移语义）、y 落在上移 SHIFT 后的矩形内；
    # 滑杆无合法收起态；canvas_rect 绑采集帧。
    collapsed = profile.tools_collapsed
    assert collapsed is not None
    assert collapsed["slider"] is None
    assert collapsed["canvas_rect"] == [40, 60, 300, 360]
    shift = ToolbarBoard.SHIFT
    for name, rect in (("undo", ToolbarBoard.UNDO), ("eraser", ToolbarBoard.ERASER)):
        assert collapsed[name][0] == open_map[name][0]  # x 不变
        shifted_rect = (rect[0], rect[1] - shift, rect[2], rect[3])
        assert _near(shifted_rect, collapsed[name]), name
    # 单套旧字段与本次探测同帧覆盖。
    assert _near(ToolbarBoard.FOLD, profile.fold)


def test_full_auto_calibrate_event_stream_shape(tmp_path, monkeypatch):
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)
    result = svc.auto_calibrate("p1", hwnd=HWND)

    events, running = svc.calibration_events("p1", 0)
    assert running is False
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    types = {event["type"] for event in events}
    assert {"calibration.warning", "calibration.geometry", "calibration.done"} <= types
    assert events[-1]["type"] == "calibration.done"
    assert "canvas" in events[-1]["payload"]["steps"]
    assert events[-1]["payload"]["calibrationId"] == result["calibrationId"]
    # manual 兜底项不进事件流；done 步骤都携带几何。
    warnings = [e for e in events if e["type"] == "calibration.warning"]
    assert all(w["payload"]["state"] != "manual" for w in warnings)
    steps_with_geometry = {
        g["payload"]["step"] for g in events if g["type"] == "calibration.geometry"
    }
    assert {"canvas", "undo", "eraser", "brush", "fold"} <= steps_with_geometry

    # 游标语义：since 之后的事件才返回。
    tail, _ = svc.calibration_events("p1", len(events) - 1)
    assert [e["seq"] for e in tail] == [len(events)]


def test_snapshot_save_failure_does_not_block(tmp_path, monkeypatch):
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)

    class BrokenImage:
        @staticmethod
        def fromarray(_arr):
            def save(*_a, **_k):
                raise OSError("disk full")

            return SimpleNamespace(save=save)

    monkeypatch.setattr(service, "Image", BrokenImage)
    result = svc.auto_calibrate("p1", hwnd=HWND)
    assert result["snapshotId"] is None
    assert result["calibrationId"] is not None
    assert store.latest_calibration("p1").snapshot_id is None


# ---------- 快照目录自愈 ----------


def test_snapshot_creates_missing_project_directory(tmp_path, monkeypatch):
    """直连 store 建项目时 uploads/<id> 不会创建（该目录原本只在 HTTP 上传链路
    建）；快照存盘必须自行补目录，而不是被 except 吞掉后静默丢快照。"""
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)
    assert not store.project_dir("p1").exists()

    result = svc.auto_calibrate("p1", hwnd=HWND)
    assert result["snapshotId"] is not None
    snapshot = store.snapshot_path("p1", result["snapshotId"])
    assert snapshot.is_file()
    with Image.open(snapshot) as image:
        assert image.format == "JPEG"
        assert image.size == (600, 700)  # 与假设备截图同帧
    assert store.latest_calibration("p1").snapshot_id == result["snapshotId"]


def test_snapshot_keeps_existing_project_directory_contents(tmp_path, monkeypatch):
    """目录已存在时 mkdir 是无害 no-op：不得清空原图/旧快照等既有文件。"""
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)
    project_dir = store.project_dir("p1")
    project_dir.mkdir(parents=True)
    original = project_dir / "original.png"
    original.write_bytes(b"fake-original")

    result = svc.auto_calibrate("p1", hwnd=HWND)
    assert result["snapshotId"] is not None
    assert original.read_bytes() == b"fake-original"
    assert store.snapshot_path("p1", result["snapshotId"]).is_file()


# ---------- 单独识别（人工兜底） ----------


def test_single_target_keeps_unselected_and_collapsed(tmp_path, monkeypatch):
    board = ToolbarBoard()
    store, svc = _make(tmp_path, board, monkeypatch)
    sentinel = {"undo": [1.0, 2.0], "slider": None}
    _seed_calibration(store, tools_collapsed=sentinel)
    before = store.latest_calibration("p1")

    result = svc.auto_calibrate("p1", targets=["undo"])
    assert result["collapsedCaptured"] is False

    after = store.latest_calibration("p1")
    open_map = after.tools_open
    # 选中项被重扫；未选中项原样保留（同帧 dx=dy=0；JSON 往返 tuple/list 统一成 list）。
    assert _near(ToolbarBoard.UNDO, open_map["undo"])
    for name in ("fold", "eraser", "brush"):
        assert [float(v) for v in open_map[name]] == [
            float(v) for v in before.tools_open[name]
        ], name
    assert open_map["slider"] == before.tools_open["slider"]
    # 不重扫收起态：旧 tools_collapsed 绝不被 None 抹掉。
    assert after.tools_collapsed == sentinel
    # manual 项不发事件：fold/slider 不应出现在 warning 流。
    events, _running = svc.calibration_events("p1", 0)
    warned = {e["payload"]["step"] for e in events if e["type"] == "calibration.warning"}
    assert "undo" in warned
    assert not {"fold", "slider"} & warned


def test_single_target_without_calibration_requires_canvas(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("p1", hwnd=HWND, targets=["undo"])
    assert (exc.value.status, exc.value.code) == (422, "E_CANVAS_MISSING")


# ---------- 并发与中止 ----------


def test_concurrent_auto_calibrate_conflicts(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    conflicts: list[BaseException | None] = []
    real_fromarray = Image.fromarray

    class GatedImage:
        """快照存盘处让主校准停住，给第二次请求确定性的撞槽窗口。"""

        @staticmethod
        def fromarray(arr):
            image = real_fromarray(arr)

            def save(*a, **k):
                image.save(*a, **k)
                thread = threading.Thread(target=lambda: conflicts.append(_second_call(svc)))
                thread.start()
                thread.join(timeout=5.0)

            return SimpleNamespace(save=save)

    monkeypatch.setattr(service, "Image", GatedImage)
    result = svc.auto_calibrate("p1", hwnd=HWND)
    assert result["calibrationId"] is not None
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], TaskError)
    assert conflicts[0].code == "E_TASK_CONFLICT"


def _second_call(svc: ExecutorService):
    try:
        svc.auto_calibrate("p1", hwnd=HWND)
    except BaseException as exc:  # noqa: BLE001  测试收集异常
        return exc
    return None


def test_stop_midway_reports_failed_event(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    requested: list[int] = []

    def stopping_detect(_image, **_kw):
        if not requested:
            requested.append(1)
            svc.calibration_stop("p1")  # 识别画布后即请求中止
        return CANVAS_DETECT

    monkeypatch.setattr(service, "detect_canvas_rect", stopping_detect)
    with pytest.raises(TaskError) as exc:
        svc.auto_calibrate("p1", hwnd=HWND)
    assert (exc.value.status, exc.value.code) == (409, "E_STOPPED")
    events, running = svc.calibration_events("p1", 0)
    assert running is False
    assert events[-1]["type"] == "calibration.failed"
    assert events[-1]["payload"]["code"] == "E_STOPPED"


def test_slot_released_after_completion(tmp_path, monkeypatch):
    board = ToolbarBoard()
    _store, svc = _make(tmp_path, board, monkeypatch)
    svc.auto_calibrate("p1", hwnd=HWND)
    # 执行槽必须已释放：再次校准不应撞 E_TASK_CONFLICT。
    assert svc.auto_calibrate("p1", hwnd=HWND)["calibrationId"] is not None


# ---------- step_to_warning 纯函数 ----------


def test_step_to_warning_mapping():
    assert step_to_warning({"step": "undo", "state": "done"})["message"] == "已识别 撤销按钮"
    failed = step_to_warning({"step": "fold", "state": "failed", "evidence": "无折叠钮"})
    assert failed["message"] == "未识别 折叠按钮：无折叠钮"
    assert step_to_warning({"step": "canvas", "state": "manual"})["message"].startswith("画布区域")
