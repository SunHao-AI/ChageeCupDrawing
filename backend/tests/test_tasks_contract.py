"""I8–I12 绘制任务与 SSE 契约测试（不触碰真实鼠标）。"""

from __future__ import annotations

import io
import re
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

    def space_pressed(self) -> bool:
        return False


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


def _wait_idle(client: TestClient, timeout: float = 5.0) -> None:
    """等执行槽位释放：终态落库在先、_release 在 finally 在后，慢机器上会放大成竞态。"""
    executor = client.app.state.executor
    deadline = time.monotonic() + timeout
    while executor.busy() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not executor.busy()


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

        _wait_idle(client)
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


def test_guard_space_pauses_drawing(tmp_path: Path, device: FakeDevice) -> None:
    """空格暂停与 ESC 同通道，stopReason 区分为 pause（前端映射「已暂停，可续画」）。"""

    class SpaceProbe(FakeProbe):
        def __init__(self, device: FakeDevice) -> None:
            super().__init__(device)
            self.space_flag = False

        def space_pressed(self) -> bool:
            return self.space_flag

    probe = SpaceProbe(device)
    mouse = YieldingMouse(on_first_drag=lambda: setattr(probe, "space_flag", True))
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
        assert final["stopReason"] == "pause"
        mouse.wait_released()
    finally:
        client.__exit__(None, None, None)


def _make_guard_client(tmp_path: Path, device: FakeDevice, mouse, probe):
    """守护专项：鼠标与探针都由用例直接持有，便于精确编排信号。"""
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
    return client


def test_guard_survives_transient_probe_errors(tmp_path: Path, device: FakeDevice) -> None:
    """信号读取的偶发异常不得终结守护：抖动若干轮后 ESC 仍要能急停。

    回归的是守护线程 `except: return` 猝死缺陷——线程一死 ESC 永久失灵，
    且用户毫无感知（表现为「程序怎么都不停」）。
    """

    class FlakyProbe(FakeProbe):
        """前 N 轮信号读取抛异常，模拟截图/窗口 API 的瞬时故障。"""

        def __init__(self, device: FakeDevice, failures: int) -> None:
            super().__init__(device)
            self.failures_left = failures

        def esc_pressed(self) -> bool:
            if self.failures_left > 0:
                self.failures_left -= 1
                raise RuntimeError("transient win32 glitch")
            return super().esc_pressed()

    probe = FlakyProbe(device, failures=5)  # 5 < GUARD_ERROR_LIMIT，应存活
    mouse = YieldingMouse(on_first_drag=lambda: setattr(probe, "esc_flag", True))
    client = _make_guard_client(tmp_path, device, mouse, probe)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"countdownMs": 0, "exec": {"guardPollMs": 10.0}},
            headers=WRITE_HEADERS,
        ).json()
        final = _wait_terminal(client, created["taskId"], timeout=10.0)
        assert final["status"] == "STOPPED"
        assert final["stopReason"] == "esc"  # 守护熬过抖动后 ESC 依然有效
        codes = [
            e.payload.get("code")
            for e in client.app.state.store.list_events(created["taskId"])
            if e.type == "task.warning"
        ]
        assert "guard_dead" not in codes  # 未达阈值不该宣告守护死亡
        mouse.wait_released()
    finally:
        client.__exit__(None, None, None)


def test_guard_gives_up_with_warning_after_repeated_errors(
    tmp_path: Path, device: FakeDevice
) -> None:
    """信号读取持续故障：连续失败达阈值后守护放弃，但必须推 guard_dead warning。

    放弃本身可接受（ESC 失灵前用户至少收到明确提示与指引），静默退出不可接受。
    """

    class BrokenProbe(FakeProbe):
        def esc_pressed(self) -> bool:
            raise RuntimeError("win32 permanently broken")

    probe = BrokenProbe(device)
    mouse = YieldingMouse()  # 首笔阻塞：守护放弃后由用户经 API 停止
    client = _make_guard_client(tmp_path, device, mouse, probe)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"countdownMs": 0, "exec": {"guardPollMs": 10.0}},
            headers=WRITE_HEADERS,
        ).json()
        task_id = created["taskId"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            codes = [
                e.payload.get("code")
                for e in client.app.state.store.list_events(task_id)
                if e.type == "task.warning"
            ]
            if "guard_dead" in codes:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("守护持续失败后未推 guard_dead warning")

        # 守护退出不能顺手误停任务（无信号≠停止）：任务仍在推进（告警到来时
        # 可能还在 ARMING 或已进入 DRAWING，取决于探测耗时），由用户显式停止。
        assert client.get(f"/api/tasks/{task_id}").json()["status"] in {"ARMING", "DRAWING"}
        assert client.post(f"/api/tasks/{task_id}/stop", headers=WRITE_HEADERS).status_code == 202
        final = _wait_terminal(client, task_id)
        assert final["stopReason"] == "user"
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


def test_delete_task_resets_to_initial_state(tmp_path: Path, device: FakeDevice) -> None:
    """删除任务回初始态：进行中 409；停止后 204，resume-state 不再有任务，规划保留。"""
    blocking = BlockingMouse()
    client = _make_client(tmp_path, device, lambda: blocking)
    try:
        plan_id, project_id = _ready_project_with_ids(client)
        task_id = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()["taskId"]
        assert blocking.entered.wait(timeout=5.0)

        denied = client.delete(f"/api/tasks/{task_id}", headers=WRITE_HEADERS)
        assert denied.status_code == 409
        assert denied.json()["errorCode"] == "E_TASK_CONFLICT"

        client.post(f"/api/tasks/{task_id}/stop", headers=WRITE_HEADERS)
        blocking.release.set()
        _wait_terminal(client, task_id)

        assert client.delete(f"/api/tasks/{task_id}", headers=WRITE_HEADERS).status_code == 204
        assert client.get(f"/api/tasks/{task_id}").status_code == 404
        state = client.get(f"/api/projects/{project_id}/resume-state").json()
        assert state["task"] is None
        # 规划与校准保留：回到「未开始绘制」而非「未规划」。
        assert state["plan"] is not None
        assert state["calibration"] is not None
        # 再删幂等报 404。
        assert client.delete(f"/api/tasks/{task_id}", headers=WRITE_HEADERS).status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------- I18 回退一笔（undo-rollback-design §3/§5） ----------


class InkingDevice(FakeDevice):
    """截图带可控墨线：撤销点击让墨线减少，回退前后帧墨量才真的不同。

    另画出滑杆轨道的深色段：轨道有墨 = 面板展开，清空流程的 PanelController
    才不会白白去点折叠钮（轨道在画布外，不影响画布检测与空白判定）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._ink_lines = 0

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        base = _screenshot(self.shift)
        if self._ink_lines:
            image = Image.fromarray(base)
            draw = ImageDraw.Draw(image)
            for row in range(0, self._ink_lines * 2, 2):
                draw.line((160, 210 + row, 440, 210 + row), fill=(0, 0, 0), width=1)
            base = np.asarray(image)
        image = Image.fromarray(base)
        ImageDraw.Draw(image).rectangle((200, 723, 400, 727), fill=(0, 0, 0))
        base = np.asarray(image)
        return CaptureResult(image=base, method="maa")

    def add_ink(self, lines: int) -> None:
        self._ink_lines = max(0, self._ink_lines + lines)


class UndoMouse(FakeMouse):
    """每次点击撤销按钮撤掉 5 行墨线（模拟画板撤销真的生效）。"""

    def __init__(self, device: InkingDevice) -> None:
        super().__init__()
        self._device = device

    def click(self, x: float, y: float, hold: float = 0.020) -> None:
        super().click(x, y, hold=hold)
        self._device.add_ink(-5)


def _stopped_task(client: TestClient, plan_id: str) -> str:
    task_id = client.post(
        f"/api/plans/{plan_id}/tasks", json={"countdownMs": 4000}, headers=WRITE_HEADERS
    ).json()["taskId"]
    assert client.post(f"/api/tasks/{task_id}/stop", headers=WRITE_HEADERS).status_code == 202
    snap = _wait_terminal(client, task_id)
    assert snap["status"] == "STOPPED"
    # STOPPED 落库早于运行线程 finally 里的槽位释放；不同步等待会偶发 409。
    _wait_idle(client)
    return task_id


def _ready_project_with_ids(client: TestClient) -> tuple[str, str]:
    """返回 (planId, projectId)，供需要回写校准的回退测试使用。"""
    project = client.post(
        "/api/projects", files={"file": ("s.png", _png(), "image/png")}, headers=WRITE_HEADERS
    ).json()
    client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "manual", "rect": {"x": 155, "y": 205, "w": 291, "h": 441}},
        headers=WRITE_HEADERS,
    )
    plan = client.post(
        f"/api/projects/{project['projectId']}/plans", json={}, headers=WRITE_HEADERS
    )
    return plan.json()["planId"], project["projectId"]


def _mark_undo(client: TestClient, project_id: str, point: dict) -> None:
    client.post(
        f"/api/projects/{project_id}/calibration",
        json={
            "hwnd": GOOD_HWND, "mode": "manual",
            "rect": {"x": 155, "y": 205, "w": 291, "h": 441},
            "undo": point,
        },
        headers=WRITE_HEADERS,
    )


def test_undo_requires_stop_point_coordinate(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id, _ = _ready_project_with_ids(client)
        task_id = _stopped_task(client, plan_id)
        # 既有校准没标撤销按钮：引导去校准页补标。
        response = client.post(f"/api/tasks/{task_id}/undo", headers=WRITE_HEADERS)
        assert response.status_code == 422
        assert response.json()["errorCode"] == "E_UNDO_UNAVAILABLE"
    finally:
        client.__exit__(None, None, None)


def test_undo_effective_and_exhausted(tmp_path: Path) -> None:
    inked = InkingDevice()
    made: list[UndoMouse] = []

    def factory() -> UndoMouse:
        mouse = UndoMouse(inked)
        made.append(mouse)
        return mouse

    client = _make_client(tmp_path, inked, factory)
    try:
        plan_id, project_id = _ready_project_with_ids(client)
        _mark_undo(client, project_id, {"x": 565, "y": 705})
        task_id = _stopped_task(client, plan_id)
        inked.add_ink(20)  # 暂停后画布上有墨：撤销「有历史」

        first = client.post(f"/api/tasks/{task_id}/undo", headers=WRITE_HEADERS)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["effective"] is True
        assert body["inkBefore"] > body["inkAfter"]
        # 校准标注的撤销点（含画布矩形平移的几像素修正）确实被点击。
        assert any(abs(x - 560) <= 8 and abs(y - 700) <= 8 for x, y in made[-1].clicks)
        # 回退不改任务状态，续画仍可用。
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "STOPPED"

        inked.add_ink(-999)  # 撤到底：画布已无墨（add_ink 自带下限 0）
        second = client.post(f"/api/tasks/{task_id}/undo", headers=WRITE_HEADERS)
        assert second.status_code == 200
        assert second.json()["effective"] is False
        assert "无墨迹" in second.json()["message"]
    finally:
        client.__exit__(None, None, None)


def test_undo_conflict_when_not_stopped(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        _ready_project(client)
        assert client.post("/api/tasks/missing/undo", headers=WRITE_HEADERS).status_code == 404
    finally:
        client.__exit__(None, None, None)


# ---------- I19 清空画布（clear-and-guide-design §4） ----------


def _clear(client: TestClient, project_id: str, body: dict | None = None):
    return client.post(
        f"/api/projects/{project_id}/clear-canvas",
        json=body or {},
        headers=WRITE_HEADERS,
    )


def test_clear_canvas_blank_short_circuit(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    """画布本来就空白：不点任何东西直接回报 already_blank。"""
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        _, project_id = _ready_project_with_ids(client)
        _mark_undo(client, project_id, {"x": 565, "y": 705})
        response = _clear(client, project_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cleared"] is True
        assert body["method"] == "already_blank"
        assert mouse_factory.made[-1].clicks == []
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_undoes_until_blank(tmp_path: Path) -> None:
    """带墨画布 → 连点撤销直到空白；STOPPED 任务不受影响。"""
    inked = InkingDevice()
    made: list[UndoMouse] = []

    def factory() -> UndoMouse:
        mouse = UndoMouse(inked)
        made.append(mouse)
        return mouse

    client = _make_client(tmp_path, inked, factory)
    try:
        plan_id, project_id = _ready_project_with_ids(client)
        _mark_undo(client, project_id, {"x": 565, "y": 705})
        task_id = _stopped_task(client, plan_id)
        inked.add_ink(20)  # 每点击撤销一次撤 5 行：4 次点完

        response = _clear(client, project_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cleared"] is True
        assert body["method"] == "undo"
        assert body["inkBefore"] > body["inkAfter"]
        assert "撤销 4 次" in body["message"]
        clicks = made[-1].clicks
        assert len(clicks) == 4
        assert all(abs(x - 560) <= 8 and abs(y - 700) <= 8 for x, y in clicks)
        assert made[-1].released >= 1
        # 清空不改任务状态：旧 STOPPED 任务留在库里作历史。
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "STOPPED"
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_plateau_reports_failure(tmp_path: Path) -> None:
    """撤销点击无效果（历史见底仍有墨）→ plateau → E_CLEAR_FAILED。"""
    inked = InkingDevice()  # 默认 FakeMouse：点击不撤墨，墨量恒不动
    client = _make_client(tmp_path, inked, lambda: FakeMouse())
    try:
        _, project_id = _ready_project_with_ids(client)
        _mark_undo(client, project_id, {"x": 565, "y": 705})
        inked.add_ink(10)

        response = _clear(client, project_id)
        assert response.status_code == 409
        assert response.json()["errorCode"] == "E_CLEAR_FAILED"
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_requires_undo_coordinate(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    """无撤销坐标 → E_CLEAR_UNAVAILABLE；授权脏探测后仍学不到 → 引导去校准页补标。"""
    inked = InkingDevice()
    client = _make_client(tmp_path, inked, mouse_factory)
    try:
        _, project_id = _ready_project_with_ids(client)
        inked.add_ink(10)
        response = _clear(client, project_id)
        assert response.status_code == 422
        assert response.json()["errorCode"] == "E_CLEAR_UNAVAILABLE"

        confirmed = _clear(client, project_id, {"confirmDirtyProbe": True})
        assert confirmed.status_code == 422
        assert confirmed.json()["errorCode"] == "E_CLEAR_UNAVAILABLE"
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_dirty_probe_unlocks_undo(tmp_path: Path) -> None:
    """§6.2：确认墨可弃后先跑脏画布探测学到撤销坐标，再继续清空直到空白。"""
    from app.executor import service as service_module
    from app.executor.toolbar_probe import ToolBitmap

    inked = InkingDevice()
    made: list[UndoMouse] = []

    def factory() -> UndoMouse:
        mouse = UndoMouse(inked)
        made.append(mouse)
        return mouse

    client = _make_client(tmp_path, inked, factory)

    class LearningProber:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, **_kwargs) -> ToolBitmap:
            # 返回「本次截图帧」下的撤销坐标；持久化时按 (dx, dy) 减回旧帧。
            return ToolBitmap(
                brush=None,
                eraser=None,
                slider=None,
                undo=(560.0, 700.0),
                fold=None,
                brush_selected_lum=None,
                brush_idle_lum=None,
            )

    original = service_module.ToolbarProber
    service_module.ToolbarProber = LearningProber
    try:
        _plan_id, project_id = _ready_project_with_ids(client)
        inked.add_ink(20)  # 未标撤销按钮：首次请求引导授权，授权后探测学坐标

        denied = _clear(client, project_id)
        assert denied.status_code == 422
        assert denied.json()["errorCode"] == "E_CLEAR_UNAVAILABLE"
        assert made[-1].clicks == []  # 未授权前绝不盲点

        response = _clear(client, project_id, {"confirmDirtyProbe": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cleared"] is True
        assert body["method"] == "undo"
        assert "撤销 4 次" in body["message"]
        assert all(abs(x - 560) <= 8 and abs(y - 700) <= 8 for x, y in made[-1].clicks)
    finally:
        service_module.ToolbarProber = original
        client.__exit__(None, None, None)


class ErasingMouse(FakeMouse):
    """撤销点击撤 5 行墨；画布内拖拽 = 橡皮行扫，一次拖拽擦净全部。"""

    def __init__(self, device: "LineInkDevice") -> None:
        super().__init__()
        self._device = device

    def click(self, x: float, y: float, hold: float = 0.020) -> None:
        super().click(x, y, hold=hold)
        self._device.undo_click()

    def drag_polyline(self, points, **kwargs) -> bool:
        pts = list(points)
        self.drags.append(pts)
        if pts:
            _CURSOR["pos"] = (round(pts[-1][0]), round(pts[-1][1]))
            # 行扫端点刻意超出画布 8px：按「水平线穿过画布」判定橡皮扫涂。
            if any(140 < x < 460 for x, _ in pts) and 200 < pts[0][1] < 650:
                self._device.erase_all()
        return True


class LineInkDevice(InkingDevice):
    """撤销只消化部分墨（模拟撤销深度不足），残留必须靠橡皮涂净。"""

    def erase_all(self) -> None:
        self._ink_lines = 0

    def undo_click(self) -> None:
        if self._ink_lines <= 10:
            return  # 撤销历史见底：剩 10 行怎么也撤不掉了
        super().add_ink(-5)


def _mark_full_toolbar(client: TestClient, project_id: str) -> None:
    """手动标定一套完整工具位图（撤销/橡皮/画笔/滑杆/折叠都在展开画布下方）。"""
    client.post(
        f"/api/projects/{project_id}/calibration",
        json={
            "hwnd": GOOD_HWND, "mode": "manual",
            "rect": {"x": 155, "y": 205, "w": 291, "h": 441},
            "undo": {"x": 565, "y": 705},
            "slider": {"x": 200, "y": 720, "w": 200, "h": 10},
            "fold": {"x": 60, "y": 725},
            "toolsOpen": {
                "brush": [80, 705],
                "eraser": [120, 705],
                "slider": {"x": 200, "y": 720, "w": 200, "h": 10},
                "undo": [560, 700],
                "fold": [60, 725],
                "brush_selected_lum": None,
                "brush_idle_lum": None,
            },
        },
        headers=WRITE_HEADERS,
    )


def test_clear_canvas_eraser_fallback_after_plateau(tmp_path: Path) -> None:
    """§4.4：撤销见底仍有残留 → 橡皮最粗档行扫涂净 → method=undo+eraser 并切回画笔。"""
    device = LineInkDevice()
    made: list[ErasingMouse] = []

    def factory() -> ErasingMouse:
        mouse = ErasingMouse(device)
        made.append(mouse)
        return mouse

    client = _make_client(tmp_path, device, factory)
    try:
        _plan_id, project_id = _ready_project_with_ids(client)
        _mark_full_toolbar(client, project_id)
        device.add_ink(40)  # 撤销只能撤到剩 10 行，其余靠橡皮

        response = _clear(client, project_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cleared"] is True
        assert body["method"] == "undo+eraser"
        assert "橡皮涂净残留" in body["message"]
        mouse = made[-1]
        # 滑杆最粗档（轨道右端内缩 12%）+ 橡皮 + 扫涂后切回画笔都发生了
        # （校准 rect 与检测画布同帧，dx=dy=0，点击即校准坐标）。
        slider_hits = [
            (x, y) for x, y in mouse.clicks
            if 200 <= x <= 395 and 720 <= y <= 730
        ]
        assert slider_hits and abs(slider_hits[0][0] - (200 + 200 * 0.88)) <= 2
        eraser_idx = next(
            i for i, (x, y) in enumerate(mouse.clicks)
            if abs(x - 120) <= 2 and abs(y - 705) <= 2
        )
        brush_idx = mouse.clicks.index((80, 705))  # 扫涂结束切回画笔
        assert brush_idx > eraser_idx
        assert mouse.drags  # 确实发生了画布内行扫
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_eraser_ink_rise_aborts(tmp_path: Path) -> None:
    """§4.4 风险缓解：扫涂第一行墨量不减反增（坐标错位画上墨）立即中止。"""

    class PaintingDevice(InkingDevice):
        """橡皮点击其实点到了画笔：行扫会画墨，首行复查必须拦住。"""

        def __init__(self) -> None:
            super().__init__()
            self.paint = False

    device = PaintingDevice()

    class PaintingMouse(FakeMouse):
        def click(self, x: float, y: float, hold: float = 0.020) -> None:
            super().click(x, y, hold=hold)
            device.paint = True  # 「橡皮」坐标其实是画笔

        def drag_polyline(self, points, **kwargs) -> bool:
            pts = list(points)
            self.drags.append(pts)
            if device.paint and pts:
                device.add_ink(40)  # 一行宽线 = 大量新墨
            return True

    client = _make_client(tmp_path, device, lambda: PaintingMouse())
    try:
        _plan_id, project_id = _ready_project_with_ids(client)
        _mark_full_toolbar(client, project_id)
        device.add_ink(30)  # 假撤销不生效（无 UndoMouse）→ plateau → 橡皮兜底

        response = _clear(client, project_id)
        assert response.status_code == 409
        assert response.json()["errorCode"] == "E_CLEAR_FAILED"
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_without_calibration(tmp_path: Path, device: FakeDevice, mouse_factory) -> None:
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        project = client.post(
            "/api/projects", files={"file": ("s.png", _png(), "image/png")}, headers=WRITE_HEADERS
        ).json()
        response = _clear(client, project["projectId"])
        assert response.status_code == 422
        assert response.json()["errorCode"] == "E_CANVAS_MISSING"
    finally:
        client.__exit__(None, None, None)


def test_clear_canvas_conflict_with_running_task(tmp_path: Path, device: FakeDevice) -> None:
    """绘制任务占用执行槽位时清空报 409，先停任务再清空成功。"""
    blocking = BlockingMouse()
    client = _make_client(tmp_path, device, lambda: blocking)
    try:
        plan_id, project_id = _ready_project_with_ids(client)
        _mark_undo(client, project_id, {"x": 565, "y": 705})
        task_id = client.post(
            f"/api/plans/{plan_id}/tasks", json={"countdownMs": 0}, headers=WRITE_HEADERS
        ).json()["taskId"]
        assert blocking.entered.wait(timeout=5.0)

        response = _clear(client, project_id)
        assert response.status_code == 409
        assert response.json()["errorCode"] == "E_TASK_CONFLICT"

        client.post(f"/api/tasks/{task_id}/stop", headers=WRITE_HEADERS)
        blocking.release.set()
        _wait_terminal(client, task_id)
        _wait_idle(client)
        assert _clear(client, project_id).status_code == 200
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


_FRONTEND_EXEC_DEFAULTS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "params" / "registry.ts"
)


def _frontend_exec_default_keys() -> set[str]:
    """从前端 defaultExecParams() 提取键集合，防止前后端字段名静默漂移。"""
    assert _FRONTEND_EXEC_DEFAULTS.is_file(), f"未找到前端默认参数文件：{_FRONTEND_EXEC_DEFAULTS}"
    source = _FRONTEND_EXEC_DEFAULTS.read_text(encoding="utf-8")
    body = re.search(
        r"export function defaultExecParams\(\): ExecParams \{\s*return \{(.*?)\n  \}",
        source,
        re.S,
    )
    assert body is not None, "未能定位 registry.ts 的 defaultExecParams() 函数体"
    keys = set(re.findall(r"^[ \t]{2,8}(\w+):", body.group(1), re.M))
    # 正则失配时键集合为空会让「集合相等」假通过，这里显式拦截。
    assert len(keys) >= 12, f"defaultExecParams() 解析出的键异常：{sorted(keys)}"
    return keys


def test_create_task_accepts_frontend_exec_payload(
    tmp_path: Path, device: FakeDevice, mouse_factory
) -> None:
    """前端 defaultExecParams() 的全部字段（camelCase）必须被后端接受并持久化。"""
    frontend_keys = _frontend_exec_default_keys()
    assert "widthTiers" not in frontend_keys

    exec_payload = {
        "eventStepPx": 1.0,
        "pointDelayMs": 1.5,
        "strokeDelayMs": 6.0,
        "clickHoldMs": 20,
        "noCoalesce": True,
        "shortStrokeProtect": True,
        "shortStrokeThresholdPx": 1.2,
        "focusGuard": True,
        "cursorTakeoverPx": 6,
        "guardPollMs": 50,
        "canvasGuard": True,
        "autoWidth": True,
        "widthTolerance": 0.05,
    }
    assert set(exec_payload) == frontend_keys, (
        f"测试 payload 与前端 defaultExecParams() 键不一致："
        f"payload={sorted(exec_payload)} frontend={sorted(frontend_keys)}"
    )

    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        created = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"exec": exec_payload, "countdownMs": 0},
            headers=WRITE_HEADERS,
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["taskId"]

        detail = client.get(f"/api/tasks/{task_id}").json()
        exec_params = detail["exec"]
        assert exec_params["widthTolerance"] == 0.05
        assert exec_params["autoWidth"] is True
        assert "widthTiers" not in exec_params
        # widthTable 由执行器回写，其余键必须与前端 payload 完全一致。
        assert {k: v for k, v in exec_params.items() if k != "widthTable"} == exec_payload
        width_table = exec_params.get("widthTable")
        assert width_table is None or isinstance(width_table, list)

        final = _wait_terminal(client, task_id)
        assert final["status"] == "DONE", final
    finally:
        client.__exit__(None, None, None)


def test_create_task_rejects_legacy_width_tiers(
    tmp_path: Path, device: FakeDevice, mouse_factory
) -> None:
    """旧前端字段 widthTiers 已不在契约中，extra=forbid 必须拒绝（400 E_INVALID_ARGUMENT）。"""
    client = _make_client(tmp_path, device, mouse_factory)
    try:
        plan_id = _ready_project(client)
        rejected = client.post(
            f"/api/plans/{plan_id}/tasks",
            json={"exec": {"widthTiers": 3}},
            headers=WRITE_HEADERS,
        )
        assert rejected.status_code == 400, rejected.text
        body = rejected.json()
        assert body["errorCode"] == "E_INVALID_ARGUMENT"
        assert any("widthTiers" in item["path"] for item in body["detail"])
    finally:
        client.__exit__(None, None, None)
