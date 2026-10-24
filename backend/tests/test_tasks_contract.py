"""I8–I12 绘制任务与 SSE 契约测试（不触碰真实鼠标）。"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import Settings
from app.device.capture import CaptureResult
from app.device.geometry import ClientArea
from app.device.windows import WindowInfo
from app.executor.service import ExecutorService
from app.main import create_app

SESSION_TOKEN = "task-test-token"
WRITE_HEADERS = {"X-Requested-With": "cupdraw"}
GOOD_HWND = 1234


def _screenshot(shift: int = 0) -> np.ndarray:
    image = Image.new("RGB", (600, 900), (200, 200, 200))
    ImageDraw.Draw(image).rectangle(
        (150 + shift, 200 + shift, 450 + shift, 650 + shift), fill=(255, 255, 255)
    )
    return np.asarray(image)


class FakeDevice:
    def __init__(self) -> None:
        self.shift = 0
        self.alive_flag = True
        self._foreground = GOOD_HWND

    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(hwnd=GOOD_HWND, title="霸王茶姬", class_name="Chagee")]

    def alive(self, hwnd: int) -> bool:
        return self.alive_flag and hwnd == GOOD_HWND

    def activate(self, hwnd: int) -> bool:
        return True

    def foreground(self) -> int:
        return self._foreground

    def title(self, hwnd: int) -> str:
        return "霸王茶姬"

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        return CaptureResult(image=_screenshot(self.shift), method="maa")

    def client_area(self, hwnd: int) -> ClientArea:
        return ClientArea(origin_x=0, origin_y=0, width=600, height=900)

    def dpi_scale(self, hwnd: int) -> float:
        return 1.0


# 模拟注入器视角下的光标：假鼠标写入，假探针读出，保证守护不误判接管。
_CURSOR = {"pos": (0, 0)}


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.drags: list[list[tuple[float, float]]] = []
        self.released = 0
        _CURSOR["pos"] = (0, 0)

    def clear_escape_state(self) -> None:
        return None

    def esc_pressed(self) -> bool:
        return False

    def click(self, x: float, y: float, hold: float = 0.020) -> None:
        self.clicks.append((x, y))
        _CURSOR["pos"] = (round(x), round(y))

    def drag_polyline(self, points, **kwargs) -> bool:
        pts = list(points)
        self.drags.append(pts)
        if pts:
            _CURSOR["pos"] = (round(pts[-1][0]), round(pts[-1][1]))
        return True

    def up(self) -> None:
        self.released += 1


class FakeProbe:
    """守护探针假实现：前台/可用性读 FakeDevice，光标读注入器落点；esc_flag 可外部置位。"""

    def __init__(self, device: FakeDevice) -> None:
        self.device = device
        self.esc_flag = False

    def clear_escape_state(self) -> None:
        return None

    def foreground_matches(self, hwnd: int) -> bool:
        return self.device.foreground() == hwnd

    def activate(self, hwnd: int) -> bool:
        # 测试前台由 FakeDevice 手动控制，不模拟自动激活。
        return False

    def window_usable(self, hwnd: int) -> bool:
        return self.device.alive(hwnd)

    def cursor_pos(self) -> tuple[int, int]:
        return _CURSOR["pos"]

    def left_button_pressed(self) -> bool:
        return False

    def esc_pressed(self) -> bool:
        return self.esc_flag


class BlockingMouse(FakeMouse):
    """第一条笔迹阻塞，便于稳定制造「已有任务在执行」的冲突窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def drag_polyline(self, points, *, should_stop=None, **kwargs) -> bool:
        self.entered.set()
        self.release.wait(timeout=5.0)
        pts = list(points)
        if pts:
            _CURSOR["pos"] = (round(pts[-1][0]), round(pts[-1][1]))
        return not (should_stop and should_stop())


class YieldingMouse(FakeMouse):
    """每条笔迹内部轮询停止标志，直到停止或超时，用于守护触发类测试。"""

    def __init__(self, on_first_drag=None) -> None:
        super().__init__()
        self.on_first_drag = on_first_drag

    def drag_polyline(self, points, *, should_stop=None, **kwargs) -> bool:
        self.drags.append(list(points))
        if self.on_first_drag and len(self.drags) == 1:
            self.on_first_drag()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            time.sleep(0.01)
        return True

    def wait_released(self, timeout: float = 5.0) -> None:
        # 线程 finally 里的 mouse.up() 晚于 STOPPED 落库，轮询等待收尾。
        deadline = time.monotonic() + timeout
        while self.released == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.released >= 1


def _png() -> bytes:
    image = Image.new("L", (300, 400), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 259, 359), outline=0, width=6)
    draw.line((40, 40, 259, 359), fill=0, width=4)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class FakePlanner:
    def run(self, *, image_path: Path, target_w: int, target_h: int, params: dict, out_dir: Path) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("mask.png", "simulated.png", "difference.png", "composite.png"):
            (out_dir / name).write_bytes(b"fake")
        # 三条可绘制折线（画布坐标），长度均大于短笔迹阈值。
        paths = [
            [[10, 10], [120, 10], [120, 90]],
            [[20, 200], [200, 260]],
            [[40, 40], [60, 300], [200, 300]],
        ]
        (out_dir / "paths.json").write_text(__import__("json").dumps(paths), encoding="utf-8")
        return {
            "ok": True,
            "strategy": "topology",
            "metrics": {
                "precision": 0.9, "recall": 0.9, "iou": 0.8,
                "pathCount": len(paths), "totalLength": 900.0,
            },
        }

    def close(self) -> None:
        return None


def _make_client(tmp_path: Path, device: FakeDevice, mouse_factory):
    app = create_app(
        Settings(data_dir=tmp_path / "data", session_token=SESSION_TOKEN),
        device=device,
        planner=FakePlanner(),
        executor_factory=lambda store, dev: ExecutorService(
            store,
            dev,
            mouse_factory=mouse_factory,
            guard_probe=FakeProbe(device),
            # 测试里不需要真实的 0.5s 前台稳定期。
            arm_final_window_s=0.05,
            arm_stable_s=0.05,
        ),
    )
    client = TestClient(app)
    client.__enter__()
    client.get("/api/health")
    return client


def _ready_project(client: TestClient) -> str:
    project = client.post(
        "/api/projects", files={"file": ("s.png", _png(), "image/png")}, headers=WRITE_HEADERS
    ).json()
    cal = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "manual", "rect": {"x": 155, "y": 205, "w": 291, "h": 441}},
        headers=WRITE_HEADERS,
    )
    assert cal.status_code == 200, cal.text
    plan = client.post(
        f"/api/projects/{project['projectId']}/plans", json={}, headers=WRITE_HEADERS
    )
    assert plan.status_code == 200, plan.text
    return plan.json()["planId"]


def _wait_terminal(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in {"DONE", "FAILED", "STOPPED"}:
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态：{body}")


@pytest.fixture()
def device() -> FakeDevice:
    return FakeDevice()


@pytest.fixture()
def mouse_factory():
    made: list[FakeMouse] = []

    def factory() -> FakeMouse:
        mouse = FakeMouse()
        made.append(mouse)
        return mouse

    factory.made = made  # type: ignore[attr-defined]
    return factory


def test_create_task_runs_to_done(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"countdownMs": 0},
            headers=WRITE_HEADERS,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] in {"ARMING", "DRAWING"}
        assert body["totalPaths"] == 3

        final = _wait_terminal(client, body["taskId"])
        assert final["status"] == "DONE"
        assert final["cursorPath"] == 3
        assert final["totalPaths"] == 3

        mouse = mouse_factory.made[0]
        assert len(mouse.drags) == 3
        # 线程 finally 里的 mouse.up() 会晚于 DONE 落库，轮询等待收尾。
        deadline = time.monotonic() + 5.0
        while mouse.released == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mouse.released >= 1
    finally:
        client.__exit__(None, None, None)


def test_create_task_conflict(tmp_path: Path, device: FakeDevice) -> None:
    blocking = BlockingMouse()
    client = _make_client(tmp_path, device, lambda: blocking)
    try:
        plan_id = _ready_project(client)
        first = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        assert blocking.entered.wait(timeout=5.0)

        second = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        )
        assert second.status_code == 409
        assert second.json()["errorCode"] == "E_TASK_CONFLICT"

        blocking.release.set()
        final = _wait_terminal(client, first["taskId"])
        assert final["status"] == "DONE"
    finally:
        client.__exit__(None, None, None)


def test_stop_during_countdown_then_resume(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 4000}, headers=WRITE_HEADERS
        ).json()
        task_id = created["taskId"]

        stopped = client.post(f"/api/tasks/{task_id}/stop", headers=WRITE_HEADERS)
        assert stopped.status_code == 202, stopped.text

        snap = _wait_terminal(client, task_id)
        assert snap["status"] == "STOPPED"
        assert snap["stopReason"] == "user"
        assert snap["cursorPath"] == 0

        resumed = client.post(
            f"/api/tasks/{task_id}/resume", json={"countdownMs": 0}, headers=WRITE_HEADERS
        )
        assert resumed.status_code == 202, resumed.text
        final = _wait_terminal(client, task_id)
        assert final["status"] == "DONE"
        assert final["cursorPath"] == 3
    finally:
        client.__exit__(None, None, None)


def test_countdown_freezes_until_foreground(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    """倒计时末段目标不在前台：冻结等待并推 warning，切回后自动继续到完成。"""
    device._foreground = 99999  # 目标不在前台
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 300}, headers=WRITE_HEADERS
        ).json()
        task_id = created["taskId"]

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            events = [e.type for e in client.app.state.store.list_events(task_id)]
            if "task.warning" in events:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("倒计时未按预期冻结推 warning")

        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "ARMING"
        assert mouse_factory.made[0].drags == []

        device._foreground = GOOD_HWND  # 用户切回目标窗口
        final = _wait_terminal(client, task_id)
        assert final["status"] == "DONE"
    finally:
        client.__exit__(None, None, None)


def test_guard_focus_lost_stops_drawing(tmp_path: Path, device: FakeDevice) -> None:
    """绘制中目标失焦：守护线程应立即停笔，STOPPED 且 stopReason=focus_lost。"""

    def lost_focus() -> None:
        device._foreground = 99999

    mouse = YieldingMouse(on_first_drag=lost_focus)
    client = _make_client(tmp_path, device, lambda: mouse)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        final = _wait_terminal(client, created["taskId"])
        assert final["status"] == "STOPPED"
        assert final["stopReason"] == "focus_lost"
        assert final["cursorPath"] == 0  # 第一条笔迹未完成即停
        assert len(mouse.drags) == 1
        mouse.wait_released()  # finally 中抬笔
    finally:
        client.__exit__(None, None, None)


def test_guard_esc_stops_drawing(tmp_path: Path, device: FakeDevice) -> None:
    """ESC 急停在守护线程并入后仍有效。"""
    probe = FakeProbe(device)
    mouse = YieldingMouse(on_first_drag=lambda: setattr(probe, "esc_flag", True))
    app = create_app(
        Settings(data_dir=tmp_path / "data", session_token=SESSION_TOKEN),
        device=device,
        planner=FakePlanner(),
        executor_factory=lambda store, dev: ExecutorService(
            store,
            dev,
            mouse_factory=lambda: mouse,
            guard_probe=probe,
            arm_final_window_s=0.05,
            arm_stable_s=0.05,
        ),
    )
    client = TestClient(app)
    client.__enter__()
    client.get("/api/health")
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        final = _wait_terminal(client, created["taskId"])
        assert final["status"] == "STOPPED"
        assert final["stopReason"] == "esc"
        mouse.wait_released()
    finally:
        client.__exit__(None, None, None)


def test_canvas_drift_blocks(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        device.shift = 40  # 落笔前画布整体偏移，IoU 明显下降
        created = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"countdownMs": 0, "allowDrift": 0.02},
            headers=WRITE_HEADERS,
        ).json()
        final = _wait_terminal(client, created["taskId"])
        assert final["status"] == "FAILED"
        assert final["errorCode"] == "E_CANVAS_DRIFT"
    finally:
        client.__exit__(None, None, None)


def test_stop_finished_task_conflict(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        _wait_terminal(client, created["taskId"])
        again = client.post(f"/api/tasks/{created['taskId']}/stop", headers=WRITE_HEADERS)
        assert again.status_code == 409
        assert again.json()["errorCode"] == "E_TASK_CONFLICT"
    finally:
        client.__exit__(None, None, None)


def test_sse_stream_replays_events(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        task_id = created["taskId"]
        _wait_terminal(client, task_id)

        with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = b"".join(response.iter_raw()).decode("utf-8")

        assert "retry: 2000" in raw
        assert "event: task.state" in raw
        assert "event: task.progress" in raw
        assert "event: task.done" in raw
        assert '"status": "DONE"' in raw
    finally:
        client.__exit__(None, None, None)


def test_sse_last_event_id_resumes(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()
        task_id = created["taskId"]
        _wait_terminal(client, task_id)

        store = client.app.state.store
        events = store.list_events(task_id)
        assert len(events) >= 2
        mid = events[0].seq

        with client.stream(
            "GET",
            f"/api/tasks/{task_id}/events",
            headers={"Last-Event-ID": str(mid)},
        ) as response:
            raw = b"".join(response.iter_raw()).decode("utf-8")

        assert f"id: {mid}\n" not in raw
        assert f"id: {events[-1].seq}" in raw
    finally:
        client.__exit__(None, None, None)


def test_task_endpoints_not_found(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        assert client.get("/api/tasks/missing").status_code == 404
        assert client.post("/api/tasks/missing/stop", headers=WRITE_HEADERS).status_code == 404
        assert client.get("/api/tasks/missing/events").status_code == 404
        missing_plan = client.post(
            "/api/plans/missing/tasks", json={}, headers=WRITE_HEADERS
        )
        assert missing_plan.status_code == 404
    finally:
        client.__exit__(None, None, None)
