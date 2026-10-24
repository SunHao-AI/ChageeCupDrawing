"""P1 接口契约测试：上传 → 校准 → 规划 → 预览。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import Settings
from app.device.capture import CaptureResult
from app.device.geometry import ClientArea
from app.device.windows import WindowInfo
from app.main import create_app
from app.services.planning import PlanFailed, PlanTimeout
from app.store.store import ASSET_FILES

SESSION_TOKEN = "contract-test-token"
WRITE_HEADERS = {"X-Requested-With": "cupdraw"}

GOOD_HWND = 1234
BLANK_HWND = 5678


def _screenshot(blank: bool = False) -> np.ndarray:
    """600×900 灰底 + 白色画布矩形，模拟霸王茶姬窗口客户区。"""
    image = Image.new("RGB", (600, 900), (200, 200, 200))
    if not blank:
        ImageDraw.Draw(image).rectangle((150, 200, 450, 650), fill=(255, 255, 255))
    return np.asarray(image)


class FakeDevice:
    """只认两个 hwnd：GOOD_HWND 有画布，BLANK_HWND 是纯灰截图。"""

    def __init__(self) -> None:
        self.capture_calls: list[tuple[int, str | None]] = []

    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(hwnd=GOOD_HWND, title="霸王茶姬", class_name="ChageeWindow")]

    def alive(self, hwnd: int) -> bool:
        return hwnd in (GOOD_HWND, BLANK_HWND)

    def activate(self, hwnd: int) -> bool:
        return True

    def title(self, hwnd: int) -> str:
        return "霸王茶姬"

    def capture(self, hwnd: int, prefer: str | None = None) -> CaptureResult:
        self.capture_calls.append((hwnd, prefer))
        return CaptureResult(image=_screenshot(blank=hwnd == BLANK_HWND), method="win32" if prefer == "win32" else "maa")

    def client_area(self, hwnd: int) -> ClientArea:
        return ClientArea(origin_x=100, origin_y=100, width=600, height=900)

    def dpi_scale(self, hwnd: int) -> float:
        return 1.25


class FakePlanner:
    """按预设结果抛错，避免错误路径真的等 120 s。"""

    def __init__(self, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def run(self, *, image_path: Path, target_w: int, target_h: int, params: dict, out_dir: Path) -> dict:
        self.calls.append(
            {
                "image_path": Path(image_path),
                "target_w": target_w,
                "target_h": target_h,
                "params": params,
                "out_dir": Path(out_dir),
            }
        )
        if self.outcome == "timeout":
            raise PlanTimeout("规划超时")
        if self.outcome == "failed":
            raise PlanFailed("掩膜为空")

        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("mask.png", "simulated.png", "difference.png", "composite.png"):
            (out_dir / name).write_bytes(b"fake")
        (out_dir / "paths.json").write_text("[[[0, 0], [1, 1]]]", encoding="utf-8")
        return {
            "ok": True,
            "strategy": "topology",
            "metrics": {
                "precision": 0.9,
                "recall": 0.9,
                "iou": 0.8,
                "pathCount": 1,
                "totalLength": 1.41,
                "correctionPaths": 0,
                "workScale": 3,
                "inkRatio": 0.1,
            },
        }

    def close(self) -> None:
        return None


def _project_png() -> bytes:
    image = Image.new("L", (300, 400), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 259, 359), outline=0, width=6)
    draw.ellipse((90, 140, 209, 259), outline=0, width=6)
    draw.line((40, 40, 259, 359), fill=0, width=4)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def app_factory(tmp_path: Path):
    def build(planner=None, device=None) -> tuple[TestClient, object]:
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                plan_timeout_s=90.0,
                session_token=SESSION_TOKEN,
            ),
            device=device or FakeDevice(),
            planner=planner,
        )
        return TestClient(app), app

    return build


@pytest.fixture()
def client(app_factory) -> TestClient:
    client, _ = app_factory()
    client.__enter__()
    client.get("/api/health")  # 首次 GET 下发会话 Cookie
    yield client
    client.__exit__(None, None, None)


def _upload(client: TestClient, *, name: str | None = "测试项目") -> dict:
    data = {"name": name} if name is not None else {}
    response = client.post(
        "/api/projects",
        files={"file": ("source.png", _project_png(), "image/png")},
        data=data,
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _calibrate(client: TestClient, project_id: str, hwnd: int = GOOD_HWND) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/calibration",
        json={"hwnd": hwnd, "mode": "auto"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _plan(client: TestClient, project_id: str) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/plans", json={}, headers=WRITE_HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------- I1 / I2 ----------


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_list_windows(client: TestClient) -> None:
    response = client.get("/api/windows")
    assert response.status_code == 200
    assert response.json() == [
        {"hwnd": GOOD_HWND, "title": "霸王茶姬", "className": "ChageeWindow"}
    ]


# ---------- 会话鉴权 ----------


def test_get_sets_session_cookie(app_factory) -> None:
    client, _ = app_factory()
    with client:
        response = client.get("/api/health")
        assert response.cookies.get("cupdraw_session") == SESSION_TOKEN
        assert "httponly" in response.headers["set-cookie"].lower()


def test_write_requires_cookie_and_header(app_factory) -> None:
    client, _ = app_factory()
    with client:
        missing_cookie = client.post("/api/presets", json={"name": "x"})
        assert missing_cookie.status_code == 401
        assert missing_cookie.json()["errorCode"] == "E_SESSION_TOKEN"

        client.get("/api/health")  # 拿到 Cookie，但仍缺自定义头
        missing_header = client.post("/api/presets", json={"name": "x"})
        assert missing_header.status_code == 401
        assert missing_header.json()["errorCode"] == "E_SESSION_TOKEN"


# ---------- I3 上传 ----------


def test_upload_project(client: TestClient) -> None:
    body = _upload(client)
    assert body["name"] == "测试项目"
    assert body["width"] == 300 and body["height"] == 400
    assert body["imageUrl"] == f"/api/projects/{body['projectId']}/image"
    assert body["createdAt"]

    image = client.get(body["imageUrl"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content


def test_upload_uses_filename_as_default_name(client: TestClient) -> None:
    body = _upload(client, name=None)
    assert body["name"] == "source"


def test_upload_rejects_oversized_payload(client: TestClient) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (20 * 1024 * 1024)
    response = client.post(
        "/api/projects",
        files={"file": ("big.png", payload, "image/png")},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 413
    assert response.json()["errorCode"] == "E_PAYLOAD_TOO_LARGE"


def test_upload_rejects_unsupported_media(client: TestClient) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 0, 0)).save(buf, format="GIF")
    response = client.post(
        "/api/projects",
        files={"file": ("anim.gif", buf.getvalue(), "image/gif")},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 415
    assert response.json()["errorCode"] == "E_UNSUPPORTED_MEDIA"


def test_upload_rejects_long_name(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        files={"file": ("source.png", _project_png(), "image/png")},
        data={"name": "长" * 65},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "E_INVALID_ARGUMENT"


def test_project_image_missing(client: TestClient) -> None:
    response = client.get("/api/projects/not-exist/image")
    assert response.status_code == 404
    assert response.json()["errorCode"] == "E_NOT_FOUND"


# ---------- I4 校准 ----------


def test_calibration_auto(client: TestClient) -> None:
    project = _upload(client)
    body = _calibrate(client, project["projectId"])
    assert body["rect"] == {"x": 155, "y": 205, "w": 291, "h": 441}
    assert body["imageWidth"] == 600 and body["imageHeight"] == 900
    assert body["dpiScale"] == 1.25
    assert body["confidence"] == 1.0
    assert body["captureMethod"] == "maa"
    assert body["calibrationId"]
    assert body["detectedAt"]


def test_calibration_auto_not_found(client: TestClient) -> None:
    project = _upload(client)
    body = _calibrate(client, project["projectId"], hwnd=BLANK_HWND)
    assert body["calibrationId"] is None
    assert body["rect"] is None
    assert body["confidence"] == 0.0


def test_calibration_manual(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "manual", "rect": {"x": 10, "y": 20, "w": 200, "h": 300}},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["rect"] == {"x": 10, "y": 20, "w": 200, "h": 300}


def test_calibration_manual_out_of_bounds(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "manual", "rect": {"x": 500, "y": 20, "w": 200, "h": 300}},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "E_INVALID_ARGUMENT"


def test_calibration_manual_requires_rect(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "manual"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400


def test_calibration_window_lost(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": 9999, "mode": "auto"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["errorCode"] == "E_WINDOW_LOST"


def test_calibration_project_missing(client: TestClient) -> None:
    response = client.post(
        "/api/projects/not-exist/calibration",
        json={"hwnd": GOOD_HWND},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["errorCode"] == "E_NOT_FOUND"


def test_calibration_rejects_invalid_mode(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={"hwnd": GOOD_HWND, "mode": "guess"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["errorCode"] == "E_INVALID_ARGUMENT"
    assert body["detail"]


# ---------- I5 / I6 / I7 规划与预览 ----------


def test_plan_requires_calibration(client: TestClient) -> None:
    project = _upload(client)
    response = client.post(
        f"/api/projects/{project['projectId']}/plans", json={}, headers=WRITE_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["errorCode"] == "E_CANVAS_MISSING"


def test_plan_requires_project(client: TestClient) -> None:
    response = client.post("/api/projects/not-exist/plans", json={}, headers=WRITE_HEADERS)
    assert response.status_code == 404


def test_plan_happy_path(client: TestClient, app_factory) -> None:
    project = _upload(client)
    calibration = _calibrate(client, project["projectId"])
    body = _plan(client, project["projectId"])

    assert body["status"] == "DONE"
    assert body["strategy"] in {"topology", "centerline", "contour", "hscan"}
    assert set(body["metrics"]) == {"precision", "recall", "iou", "pathCount", "totalLength"}
    assert body["metrics"]["pathCount"] > 0
    rect = calibration["rect"]
    assert body["targetSize"] == {"w": rect["w"], "h": rect["h"]}
    assert set(body["assets"]) == set(ASSET_FILES)

    plan_id = body["planId"]
    store = client.app.state.store
    for kind in ASSET_FILES:
        response = client.get(body["assets"][kind])
        assert response.status_code == 200, kind
        assert response.content
        assert store.plan_asset_path(plan_id, kind).is_file()

    paths = json.loads(store.plan_paths_path(plan_id).read_text(encoding="utf-8"))
    assert paths and all(len(path) >= 2 for path in paths)

    detail = client.get(f"/api/plans/{plan_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["planId"] == plan_id
    assert payload["params"]["imageMode"] == "fidelity"
    assert payload["createdAt"]


def test_plan_detail_missing(client: TestClient) -> None:
    response = client.get("/api/plans/not-exist")
    assert response.status_code == 404
    assert response.json()["errorCode"] == "E_NOT_FOUND"


def test_plan_invalid_asset_kind(client: TestClient) -> None:
    response = client.get("/api/plans/not-exist/assets/bogus")
    assert response.status_code == 404


def test_plan_timeout(app_factory) -> None:
    client, _ = app_factory(planner=FakePlanner("timeout"))
    with client:
        client.get("/api/health")
        project = _upload(client)
        _calibrate(client, project["projectId"])
        response = client.post(
            f"/api/projects/{project['projectId']}/plans", json={}, headers=WRITE_HEADERS
        )
    assert response.status_code == 504
    assert response.json()["errorCode"] == "E_PLAN_TIMEOUT"


def test_plan_failed(app_factory) -> None:
    client, _ = app_factory(planner=FakePlanner("failed"))
    with client:
        client.get("/api/health")
        project = _upload(client)
        _calibrate(client, project["projectId"])
        response = client.post(
            f"/api/projects/{project['projectId']}/plans", json={}, headers=WRITE_HEADERS
        )
    assert response.status_code == 422
    assert response.json()["errorCode"] == "E_PLAN_FAILED"


def test_plan_passes_params_through(app_factory) -> None:
    planner = FakePlanner()
    client, _ = app_factory(planner=planner)
    with client:
        client.get("/api/health")
        project = _upload(client)
        calibration = _calibrate(client, project["projectId"])
        response = client.post(
            f"/api/projects/{project['projectId']}/plans",
            json={"params": {"strategy": "centerline", "brushWidth": 5}},
            headers=WRITE_HEADERS,
        )
    assert response.status_code == 200, response.text
    sent = planner.calls[0]
    assert sent["target_w"] == calibration["rect"]["w"]
    assert sent["target_h"] == calibration["rect"]["h"]
    assert sent["params"]["strategy"] == "centerline"
    assert sent["params"]["brushWidth"] == 5
    assert sent["params"]["imageMode"] == "fidelity"


def test_plan_rejects_out_of_range_param(app_factory) -> None:
    client, _ = app_factory(planner=FakePlanner())
    with client:
        client.get("/api/health")
        project = _upload(client)
        response = client.post(
            f"/api/projects/{project['projectId']}/plans",
            json={"params": {"threshold": 300}},
            headers=WRITE_HEADERS,
        )
    assert response.status_code == 400
    assert response.json()["errorCode"] == "E_INVALID_ARGUMENT"


# ---------- I13 预设 ----------


def test_presets_roundtrip(client: TestClient) -> None:
    assert client.get("/api/presets").json() == []

    created = client.post(
        "/api/presets",
        json={"name": "线稿", "params": {"strategy": "centerline", "brushWidth": 2}},
        headers=WRITE_HEADERS,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "线稿"
    assert body["builtin"] is False
    assert body["params"]["strategy"] == "centerline"
    assert body["params"]["brushWidth"] == 2

    listed = client.get("/api/presets").json()
    assert [preset["presetId"] for preset in listed] == [body["presetId"]]


def test_preset_rejects_blank_name(client: TestClient) -> None:
    response = client.post("/api/presets", json={"name": "   "}, headers=WRITE_HEADERS)
    assert response.status_code == 400
    assert response.json()["errorCode"] == "E_INVALID_ARGUMENT"


# ---------- I14 / I15 线稿预留 ----------


def test_lineart_placeholder(client: TestClient) -> None:
    assert client.get("/api/lineart/providers").json() == []
    response = client.post("/api/lineart/generate", json={}, headers=WRITE_HEADERS)
    assert response.status_code == 501
    assert response.json()["errorCode"] == "E_NOT_IMPLEMENTED"


# ---------- 双态工具位图透传（逐段笔宽 Task 2） ----------


def test_calibration_accepts_tools_bitmaps(client: TestClient) -> None:
    project = _upload(client)
    tools_open = {"brush": {"x": 10, "y": 500}, "brushSelectedLum": 180.0}
    response = client.post(
        f"/api/projects/{project['projectId']}/calibration",
        json={
            "hwnd": GOOD_HWND,
            "mode": "auto",
            "toolsOpen": tools_open,
            "toolsCollapsed": None,
        },
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["toolsOpen"] == tools_open
    assert body["toolsCollapsed"] is None

    profile = client.app.state.store.latest_calibration(project["projectId"])
    assert profile.tools_open == tools_open
    assert profile.tools_collapsed is None


def test_calibration_without_tools_bitmaps_returns_none(client: TestClient) -> None:
    """旧前端不带新字段：照常保存，响应回 None（旧校准记录兼容）。"""
    project = _upload(client)
    body = _calibrate(client, project["projectId"])
    assert body["toolsOpen"] is None
    assert body["toolsCollapsed"] is None
