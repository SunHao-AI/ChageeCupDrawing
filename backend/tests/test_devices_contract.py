"""B2 设备/投屏接口契约：统一枚举、登记窗口、scrcpy 缺失路径、目标类型贯通。"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import Settings
from app.device.capture import CaptureResult
from app.device.geometry import ClientArea
from app.device.mirror import MirrorService
from app.device.windows import WindowInfo
from app.main import create_app

SESSION_TOKEN = "devices-test-token"
WRITE_HEADERS = {"X-Requested-With": "cupdraw"}

GOOD_HWND = 1234
MIRROR_HWND = 9999


def _screenshot() -> np.ndarray:
    image = Image.new("RGB", (600, 900), (200, 200, 200))
    ImageDraw.Draw(image).rectangle((150, 200, 450, 650), fill=(255, 255, 255))
    return np.asarray(image)


class FakeDevice:
    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(hwnd=GOOD_HWND, title="霸王茶姬", class_name="ChageeWindow")]

    def alive(self, hwnd: int) -> bool:
        return hwnd in (GOOD_HWND, MIRROR_HWND)

    def activate(self, hwnd: int) -> bool:
        return True

    def title(self, hwnd: int) -> str:
        return "CupDraw Mirror - PHONE1" if hwnd == MIRROR_HWND else "霸王茶姬"

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        return CaptureResult(image=_screenshot(), method="maa")

    def client_area(self, hwnd: int) -> ClientArea:
        return ClientArea(origin_x=100, origin_y=100, width=600, height=900)

    def dpi_scale(self, hwnd: int) -> float:
        return 1.0


class DeadDevice(FakeDevice):
    def alive(self, _hwnd: int) -> bool:
        return False


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        Settings(data_dir=tmp_path / "data", session_token=SESSION_TOKEN),
        device=FakeDevice(),
    )
    # 固定工具与设备状态，避免断言依赖开发机 PATH 与真机连接。
    app.state.mirror.tools_available = lambda: (False, ["adb", "scrcpy"])  # type: ignore[method-assign]
    app.state.mirror.devices = lambda: [  # type: ignore[method-assign]
        {"serial": "PHONE1", "model": "Pixel 7"}
    ]
    client = TestClient(app)
    client.__enter__()
    client.get("/api/health")  # 首次 GET 下发会话 Cookie
    yield client
    client.__exit__(None, None, None)


def _upload(client: TestClient) -> str:
    buf = io.BytesIO()
    Image.fromarray(_screenshot()).save(buf, format="PNG")
    buf.seek(0)
    res = client.post(
        "/api/projects", files={"file": ("p.png", buf, "image/png")}, headers=WRITE_HEADERS
    )
    assert res.status_code == 201
    return res.json()["projectId"]


def test_devices_lists_windows_android_and_tools(client: TestClient) -> None:
    body = client.get("/api/devices").json()
    assert body["pcWindows"][0]["hwnd"] == GOOD_HWND
    assert body["android"] == [{"serial": "PHONE1", "model": "Pixel 7"}]
    assert body["tools"] == {"available": False, "missing": ["adb", "scrcpy"]}
    assert body["mirror"]["running"] is False


def test_register_mirror_window_then_status(client: TestClient) -> None:
    res = client.post(
        "/api/devices/mirror/register",
        json={"hwnd": MIRROR_HWND, "serial": "PHONE1", "label": "Pixel 7"},
        headers=WRITE_HEADERS,
    )
    assert res.status_code == 200
    assert res.json() == {
        "serial": "PHONE1",
        "label": "Pixel 7",
        "hwnd": MIRROR_HWND,
        "running": True,
        "window_title": "CupDraw Mirror - PHONE1",
    }
    assert client.get("/api/devices").json()["mirror"]["running"] is True


def test_register_missing_window_rejected(client: TestClient) -> None:
    res = client.post(
        "/api/devices/mirror/register",
        json={"hwnd": 424242, "serial": "PHONE1"},
        headers=WRITE_HEADERS,
    )
    assert res.status_code == 409
    assert res.json()["errorCode"] == "E_WINDOW_LOST"


def test_start_mirror_without_tools_reports_missing(client: TestClient) -> None:
    res = client.post(
        "/api/devices/mirror", json={"serial": "PHONE1"}, headers=WRITE_HEADERS
    )
    assert res.status_code == 503
    assert res.json()["errorCode"] == "E_SCRCPY_MISSING"


def test_stop_mirror_clears_registry(client: TestClient) -> None:
    client.post(
        "/api/devices/mirror/register",
        json={"hwnd": MIRROR_HWND, "serial": "PHONE1"},
        headers=WRITE_HEADERS,
    )
    res = client.delete("/api/devices/mirror", headers=WRITE_HEADERS)
    assert res.status_code == 200
    assert res.json()["running"] is False


def test_calibration_records_mirror_target_kind(client: TestClient) -> None:
    project_id = _upload(client)
    res = client.post(
        f"/api/projects/{project_id}/calibration",
        json={
            "hwnd": MIRROR_HWND,
            "mode": "auto",
            "deviceKind": "mirror",
            "deviceSerial": "PHONE1",
        },
        headers=WRITE_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["deviceKind"] == "mirror"
    assert body["deviceSerial"] == "PHONE1"

    from app.device.target import MirrorTarget, Win32Target, resolve_target

    store = client.app.state.store  # type: ignore[attr-defined]
    profile = store.latest_calibration(project_id)
    assert profile is not None and profile.device_kind == "mirror"
    target = resolve_target(profile, client.app.state.device, probe=object())  # type: ignore[attr-defined]
    assert isinstance(target, MirrorTarget)
    assert target.kind == "mirror" and "scrcpy" in target.lost_hint

    legacy = store.save_calibration(
        project_id=project_id, hwnd=GOOD_HWND, window_title="霸王茶姬",
        capture_method="maa", rect_x=1, rect_y=1, rect_w=10, rect_h=10,
        dpi_scale=1.0, source="manual", confidence=1.0,
    )
    assert legacy.device_kind == "pc"
    assert isinstance(
        resolve_target(legacy, client.app.state.device, probe=object()), Win32Target  # type: ignore[attr-defined]
    )


def test_mirror_service_persists_across_instances(tmp_path: Path) -> None:
    device = FakeDevice()
    first = MirrorService(tmp_path / "data", device)
    first.register_window(hwnd=MIRROR_HWND, serial="PHONE1", label="Pixel 7")

    second = MirrorService(tmp_path / "data", device)
    status = second.status()
    assert status.running is True and status.hwnd == MIRROR_HWND

    assert MirrorService(tmp_path / "data", DeadDevice()).status().running is False
