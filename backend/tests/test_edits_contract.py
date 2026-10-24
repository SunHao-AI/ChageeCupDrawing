"""VLM 编辑接口契约测试：I16–I21 + 语义金标准 + 版本链 + 墨量护栏 + BYOK 分流。"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.api.routes_settings import _merge_probe_settings
from app.api.schemas import AiProbeRequest
from app.config import Settings
from app.main import create_app
from app.services.editing import BuiltinSemanticProvider, EditOp, EditService, NoGenerativeProvider
from app.services.settings_store import KEY_HEADER, AiSettings

SESSION_TOKEN = "edits-test-token"
WRITE_HEADERS = {"X-Requested-With": "cupdraw"}


class NoopDevice:
    """编辑链路不触碰设备，只满足装配依赖。"""

    def alive(self, hwnd: int) -> bool:
        return False


def _source_png() -> bytes:
    """300×400 白底 + 黑色方框与对角线，前景占比约 8%。"""
    image = Image.new("L", (300, 400), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 259, 359), outline=0, width=6)
    draw.line((40, 40, 259, 359), fill=0, width=4)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class GenerativeProvider:
    """已注册但模型后端未配置的生成 provider，用于验证 501。"""

    id = "fake-generative"
    label = "Fake Generative"
    supports = "generative"

    def parse(self, instruction: str) -> list[EditOp]:
        return []

    def apply(self, image: Path, ops: list[EditOp], out_path: Path) -> Path:
        raise AssertionError("生成 provider 不应执行语义 apply")

    def generate(
        self, image: Path, roi: dict | None, instruction: str, out_path: Path
    ) -> Path:
        raise NoGenerativeProvider("模型后端未配置")


class CloudProvider:
    """BYOK 云端 provider 桩：generate 直接复制源图，验证分流而非模型效果。"""

    id = "cloud-fake"
    label = "Cloud Fake"
    supports = "generative"
    cloud = True
    needs_key = True

    def parse(self, instruction: str) -> list[EditOp]:
        raise AssertionError("云端 provider 不负责语义解析")

    def apply(self, image: Path, ops: list[EditOp], out_path: Path) -> Path:
        raise AssertionError("云端 provider 不应执行语义 apply")

    def generate(
        self, image: Path, roi: dict | None, instruction: str, out_path: Path
    ) -> Path:
        import shutil

        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, out_path)
        return out_path


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        Settings(data_dir=tmp_path / "data", session_token=SESSION_TOKEN),
        device=NoopDevice(),  # type: ignore[arg-type]
    )
    with TestClient(app) as test_client:
        test_client.get("/api/health")
        yield test_client


@pytest.fixture()
def project(client: TestClient) -> dict:
    response = client.post(
        "/api/projects",
        files={"file": ("s.png", _source_png(), "image/png")},
        data={"name": "编辑测试"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _edit(client: TestClient, project_id: str, payload: dict) -> tuple[int, dict]:
    response = client.post(
        f"/api/projects/{project_id}/edits", json=payload, headers=WRITE_HEADERS
    )
    return response.status_code, response.json()


# ---------- I16 ----------


def test_providers_lists_builtin_semantic(client: TestClient) -> None:
    response = client.get("/api/edit/providers")
    assert response.status_code == 200
    providers = response.json()
    assert any(p["id"] == "builtin-semantic" and p["kind"] == "semantic" for p in providers)


# ---------- I17 语义通道 ----------


def test_create_semantic_draft_and_fetch_images(client: TestClient, project: dict) -> None:
    status, body = _edit(
        client,
        project["projectId"],
        {"instruction": "上移30", "baseVersion": 1},
    )
    assert status == 201, body
    assert body["channel"] == "semantic"
    assert body["parsedOps"] == [{"op": "translate", "dx": 0, "dy": -30}]
    assert body["appliedAt"] is None

    draft = client.get(body["draftUrl"])
    assert draft.status_code == 200
    assert Image.open(io.BytesIO(draft.content)).size == (300, 400)
    diff = client.get(body["diffUrl"])
    assert diff.status_code == 200
    assert Image.open(io.BytesIO(diff.content)).mode == "RGB"


def test_draft_404_after_discard(client: TestClient, project: dict) -> None:
    _, body = _edit(client, project["projectId"], {"instruction": "加粗2", "baseVersion": 1})
    draft_url = body["draftUrl"]
    assert client.get(draft_url).status_code == 200

    response = client.delete(f"/api/edits/{body['editId']}", headers=WRITE_HEADERS)
    assert response.status_code == 200
    assert client.get(draft_url).status_code == 404


def test_unparsed_instruction_returns_422(client: TestClient, project: dict) -> None:
    # 默认装配已注册生成 provider，未配置时引导去设置页而不是「换说法」。
    status, body = _edit(
        client, project["projectId"], {"instruction": "帮我画得好看一点", "baseVersion": 1}
    )
    assert status == 422
    assert body["errorCode"] == "E_PROVIDER_NOT_CONFIGURED"


def test_roi_out_of_bounds_rejected(client: TestClient, project: dict) -> None:
    status, body = _edit(
        client,
        project["projectId"],
        {
            "instruction": "擦除",
            "baseVersion": 1,
            "roi": {"x": 250, "y": 350, "w": 100, "h": 100},
        },
    )
    assert status == 400
    assert body["errorCode"] == "E_INVALID_ARGUMENT"


def test_stale_base_version_rejected(client: TestClient, project: dict) -> None:
    status, body = _edit(
        client, project["projectId"], {"instruction": "上移30", "baseVersion": 7}
    )
    assert status == 409
    assert body["errorCode"] == "E_VERSION_STALE"


def test_unknown_provider_rejected(client: TestClient, project: dict) -> None:
    status, body = _edit(
        client,
        project["projectId"],
        {"instruction": "上移30", "baseVersion": 1, "provider": "nope"},
    )
    assert status == 400
    assert body["errorCode"] == "E_INVALID_ARGUMENT"


def test_generative_provider_without_backend_returns_501(
    client: TestClient, project: dict
) -> None:
    from app.services.editing import EditService

    client.app.state.editor = EditService([GenerativeProvider()])  # type: ignore[attr-defined]
    status, body = _edit(
        client,
        project["projectId"],
        {"instruction": "去掉右下角水印", "baseVersion": 1, "provider": "fake-generative"},
    )
    assert status == 501
    assert body["errorCode"] == "E_NOT_IMPLEMENTED"


# ---------- I18 版本链 ----------


def test_apply_bumps_version_chain(client: TestClient, project: dict) -> None:
    _, body = _edit(client, project["projectId"], {"instruction": "上移30", "baseVersion": 1})
    response = client.post(
        f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS
    )
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["sourceVersion"] == 2
    assert applied["parentVersion"] == 1
    assert applied["edit"]["appliedAt"] is not None

    # 原图回读指向新版本，且基于旧版本的新编辑被拒绝。
    image = client.get(applied["imageUrl"])
    assert image.status_code == 200
    status, body = _edit(client, project["projectId"], {"instruction": "下移20", "baseVersion": 1})
    assert status == 409

    # 重复应用同一草稿被拒绝。
    repeat = client.post(
        f"/api/edits/{body.get('editId', applied['edit']['editId'])}/apply",
        json={},
        headers=WRITE_HEADERS,
    )
    assert repeat.status_code in (404, 409)


def test_apply_twice_same_draft_rejected(client: TestClient, project: dict) -> None:
    _, body = _edit(client, project["projectId"], {"instruction": "镜像", "baseVersion": 1})
    first = client.post(f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS)
    assert first.status_code == 200
    second = client.post(
        f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS
    )
    assert second.status_code == 409
    assert second.json()["errorCode"] == "E_EDIT_APPLIED"


def test_apply_blocked_by_ink_guard(client: TestClient, project: dict) -> None:
    # 擦除整图 → 墨量 0%，必须阻断在应用之前。
    status, body = _edit(
        client, project["projectId"], {"instruction": "擦除", "baseVersion": 1}
    )
    assert status == 201, body
    response = client.post(
        f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["errorCode"] == "E_EDIT_INK_OUT_OF_RANGE"

    # 被护栏拦下后项目仍停留在 v1。
    status, draft = _edit(client, project["projectId"], {"instruction": "上移10", "baseVersion": 1})
    assert status == 201


def test_apply_after_project_deleted(client: TestClient, project: dict) -> None:
    _, body = _edit(client, project["projectId"], {"instruction": "上移30", "baseVersion": 1})
    # 项目删除接口未开放给 HTTP（生命周期由导入页管理），直接走 store 模拟级联清理。
    client.app.state.store.delete_project(project["projectId"])  # type: ignore[attr-defined]
    apply = client.post(
        f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS
    )
    assert apply.status_code == 404


# ---------- I22 版本历史与复原 ----------


def test_versions_and_revert_roundtrip(client: TestClient, project: dict) -> None:
    project_id = project["projectId"]
    before = client.get(f"/api/projects/{project_id}/versions")
    assert before.status_code == 200
    assert [v["version"] for v in before.json()] == [1]
    assert before.json()[0]["label"] == "初始导入"
    assert before.json()[0]["current"] is True

    # 应用一次编辑 → v2，历史含两个版本，标签来自编辑指令。
    _, body = _edit(client, project_id, {"instruction": "上移30", "baseVersion": 1})
    applied = client.post(f"/api/edits/{body['editId']}/apply", json={}, headers=WRITE_HEADERS)
    assert applied.status_code == 200, applied.text
    history = client.get(f"/api/projects/{project_id}/versions").json()
    assert [v["version"] for v in history] == [2, 1]
    assert history[0]["label"] == "上移30"
    assert history[1]["label"] == "初始导入"

    # 复原到 v1：内容另存为 v3，历史保留 v2 可来回。
    reverted = client.post(
        f"/api/projects/{project_id}/revert",
        json={"targetVersion": 1},
        headers=WRITE_HEADERS,
    )
    assert reverted.status_code == 200, reverted.text
    result = reverted.json()
    assert result["sourceVersion"] == 3
    assert result["revertedFrom"] == 1

    history = client.get(f"/api/projects/{project_id}/versions").json()
    assert [v["version"] for v in history] == [3, 2, 1]
    assert history[0]["label"] == "复原至 v1"
    assert history[0]["current"] is True

    # v3 与 v1 的像素内容一致。
    image_v3 = client.get(f"/api/projects/{project_id}/image")
    store = client.app.state.store  # type: ignore[attr-defined]
    files = sorted(store.project_dir(project_id).iterdir())
    v1 = next(p for p in files if p.name == "source_v1.png")
    assert np.array_equal(_ink_of(image_v3.content), _ink_of(v1.read_bytes()))

    # 复原到当前版本被拒；不存在的版本 404。
    same = client.post(
        f"/api/projects/{project_id}/revert",
        json={"targetVersion": 3},
        headers=WRITE_HEADERS,
    )
    assert same.status_code == 409
    missing = client.post(
        f"/api/projects/{project_id}/revert",
        json={"targetVersion": 9},
        headers=WRITE_HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["errorCode"] == "E_VERSION_MISSING"


# ---------- 语义金标准 ----------


def _ink_of(response_content: bytes) -> np.ndarray:
    return (np.asarray(Image.open(io.BytesIO(response_content)).convert("L")) < 128).astype(
        np.uint8
    )


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 1.0


def _shift(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """平移且边缘补 0，避免 np.roll 环绕产生伪影。"""
    out = np.zeros_like(a)
    h, w = a.shape
    ys, yd = slice(max(0, dy), min(h, h + dy)), slice(max(0, -dy), min(h, h - dy))
    xs, xd = slice(max(0, dx), min(w, w + dx)), slice(max(0, -dx), min(w, w - dx))
    out[ys, xs] = a[yd, xd]
    return out


def test_semantic_base_is_latest_plan_mask(client: TestClient, project: dict) -> None:
    """有规划结果时，语义编辑作用于最新掩膜而非源图（与预览同坐标系）。"""
    store = client.app.state.store  # type: ignore[attr-defined]
    project_id = project["projectId"]
    store.save_calibration(
        project_id=project_id, hwnd=1, window_title="t", capture_method="win32",
        rect_x=0, rect_y=0, rect_w=150, rect_h=200, dpi_scale=1.0,
        source="manual", confidence=1.0,
    )
    plan_id = store.new_plan_id()
    plan_dir = store.plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)
    mask = Image.new("L", (150, 200), 255)
    ImageDraw.Draw(mask).rectangle((10, 10, 139, 189), outline=0, width=4)
    mask_path = plan_dir / "mask.png"
    mask.save(mask_path)
    store.create_plan(
        plan_id=plan_id, project_id=project_id, params={}, strategy="topology",
        metrics={}, paths_path=plan_dir / "paths.json", target_w=150, target_h=200,
    )

    _, body = _edit(client, project_id, {"instruction": "镜像", "baseVersion": 1})
    draft = client.get(body["draftUrl"])
    assert draft.status_code == 200
    # 结果尺寸跟随掩膜（画布尺寸），而不是源图 300×400。
    assert Image.open(io.BytesIO(draft.content)).size == (150, 200)


def test_semantic_golden_standard(client: TestClient, project: dict) -> None:
    """「上移50」「镜像」「加粗2」与独立 numpy 参考实现 IoU ≥ 0.99。"""
    base_ink = _ink_of(_source_png())

    reference_up = _shift(base_ink, -50, 0)
    reference_mirror = base_ink[:, ::-1]
    reference_thicken = base_ink.copy()
    # 与 cv2 MORPH_ELLIPSE 5×5 核一致：切比雪夫距离 ≤2 但排除四个角。
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if (dy, dx) in ((2, 2), (2, -2), (-2, 2), (-2, -2)):
                continue
            reference_thicken |= _shift(base_ink, dy, dx)

    for instruction, reference in (
        ("上移50", reference_up),
        ("镜像", reference_mirror),
        ("加粗2", reference_thicken),
    ):
        _, body = _edit(client, project["projectId"], {"instruction": instruction, "baseVersion": 1})
        assert body.get("parsedOps"), body
        draft = client.get(body["draftUrl"])
        assert draft.status_code == 200
        assert _iou(_ink_of(draft.content), reference) >= 0.99, instruction


# ---------- BYOK 分流矩阵（设计文档 ai-edit-provider §2） ----------

_CLOUD_KEY = "sk-test-0123456789abcdef"


@pytest.fixture()
def cloud_client(client: TestClient) -> TestClient:
    """注册云端 provider 桩；Key/授权保持未配置，由用例逐项开启。"""
    client.app.state.editor = EditService(  # type: ignore[attr-defined]
        [BuiltinSemanticProvider(), CloudProvider()],
        settings=client.app.state.ai_settings,  # type: ignore[attr-defined]
    )
    return client


def _configure_ai(client: TestClient, *, key: bool, consent: bool) -> None:
    # Key 不再走请求体，改为每次请求带 X-AI-Key（浏览器侧持有）。
    response = client.put(
        "/api/settings/ai",
        json={
            "provider": "cloud-fake" if key else None,
            "allowCloudEdit": consent,
        },
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200, response.text
    if key:
        client.headers[KEY_HEADER] = _CLOUD_KEY
    else:
        client.headers.pop(KEY_HEADER, None)


def test_rule_hit_never_touches_cloud(cloud_client: TestClient, project: dict) -> None:
    """规则命中的高频指令必须留在语义通道，即使云端已配置。"""
    _configure_ai(cloud_client, key=True, consent=True)
    status, body = _edit(
        cloud_client, project["projectId"], {"instruction": "上移30", "baseVersion": 1}
    )
    assert status == 201, body
    assert body["channel"] == "semantic"
    assert body["provider"] == "builtin-semantic"


def test_rule_miss_falls_back_to_generative(cloud_client: TestClient, project: dict) -> None:
    _configure_ai(cloud_client, key=True, consent=True)
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "把头发改密并加上春字艺术字", "baseVersion": 1},
    )
    assert status == 201, body
    assert body["channel"] == "generative"
    assert body["provider"] == "cloud-fake"
    assert cloud_client.get(body["draftUrl"]).status_code == 200


def test_generative_channel_bypasses_rules(cloud_client: TestClient, project: dict) -> None:
    """显式 channel=generative 时即使规则可命中也走生成通道。"""
    _configure_ai(cloud_client, key=True, consent=True)
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "上移30", "baseVersion": 1, "channel": "generative"},
    )
    assert status == 201, body
    assert body["channel"] == "generative"


def test_semantic_channel_never_falls_back(cloud_client: TestClient, project: dict) -> None:
    _configure_ai(cloud_client, key=True, consent=True)
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "把头发改密", "baseVersion": 1, "channel": "semantic"},
    )
    assert status == 422
    assert body["errorCode"] == "E_EDIT_NOT_UNDERSTOOD"


def test_rule_miss_without_key_returns_422(cloud_client: TestClient, project: dict) -> None:
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "把头发改密", "baseVersion": 1},
    )
    assert status == 422
    assert body["errorCode"] == "E_PROVIDER_NOT_CONFIGURED"


def test_rule_miss_without_consent_returns_403(cloud_client: TestClient, project: dict) -> None:
    _configure_ai(cloud_client, key=True, consent=False)
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "把头发改密", "baseVersion": 1},
    )
    assert status == 403
    assert body["errorCode"] == "E_CONSENT_REQUIRED"


def test_builtin_only_miss_stays_422(client: TestClient, project: dict) -> None:
    """未注册任何生成 provider 时规则未命中维持 422「未理解」，而不是 501。"""
    client.app.state.editor = EditService(  # type: ignore[attr-defined]
        [BuiltinSemanticProvider()], settings=client.app.state.ai_settings  # type: ignore[attr-defined]
    )
    status, body = _edit(
        client, project["projectId"], {"instruction": "把头发改密", "baseVersion": 1}
    )
    assert status == 422
    assert body["errorCode"] == "E_EDIT_NOT_UNDERSTOOD"


def test_explicit_cloud_provider_ignores_rules(cloud_client: TestClient, project: dict) -> None:
    _configure_ai(cloud_client, key=True, consent=True)
    status, body = _edit(
        cloud_client,
        project["projectId"],
        {"instruction": "上移30", "baseVersion": 1, "provider": "cloud-fake"},
    )
    assert status == 201, body
    assert body["channel"] == "generative"


def test_providers_expose_cloud_flags(cloud_client: TestClient) -> None:
    providers = {p["id"]: p for p in cloud_client.get("/api/edit/providers").json()}
    assert providers["builtin-semantic"]["local"] is True
    assert providers["builtin-semantic"]["needsKey"] is False
    assert providers["cloud-fake"]["cloud"] is True
    assert providers["cloud-fake"]["needsKey"] is True


# ---------- I20 / I21 设置 ----------


def test_ai_settings_roundtrip_never_accepts_key(client: TestClient) -> None:
    """I20 只存 provider/模型/地址；请求体里的 Key 被忽略，接口不回显。"""
    initial = client.get("/api/settings/ai")
    assert initial.status_code == 200
    assert initial.json()["hasKey"] is False

    saved = client.put(
        "/api/settings/ai",
        json={
            "provider": "cloud-fake",
            "apiKey": _CLOUD_KEY,
            "baseUrl": "http://127.0.0.1:11434/v1",
            "allowCloudEdit": True,
        },
        headers=WRITE_HEADERS,
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["hasKey"] is False
    assert body["apiKeyMasked"] is None
    assert body["provider"] == "cloud-fake"
    assert body["baseUrl"] == "http://127.0.0.1:11434/v1"
    assert body["allowCloudEdit"] is True
    assert _CLOUD_KEY not in saved.text

    patched = client.put(
        "/api/settings/ai",
        json={"provider": "", "allowCloudEdit": False},
        headers=WRITE_HEADERS,
    )
    body = patched.json()
    assert body["hasKey"] is False
    assert body["provider"] is None
    assert body["allowCloudEdit"] is False


def test_ai_settings_key_never_reaches_disk(client: TestClient, tmp_path: Path) -> None:
    """Key 只存浏览器：即使旧客户端提交明文，也不能落盘。"""
    client.put(
        "/api/settings/ai",
        json={"provider": "cloud-fake", "apiKey": _CLOUD_KEY, "allowCloudEdit": True},
        headers=WRITE_HEADERS,
    )
    import json

    raw = json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))
    assert raw.get("apiKey", "") == ""
    assert _CLOUD_KEY not in (tmp_path / "data" / "config.json").read_text(encoding="utf-8")


def test_stored_key_is_purged_on_startup(client: TestClient, tmp_path: Path) -> None:
    """旧版本遗留的 Key 在启动时清除。"""
    config = tmp_path / "data" / "config.json"
    config.write_text(
        '{"provider": "cloud-fake", "model": "m", "apiKey": "sk-old", "baseUrl": ""}',
        encoding="utf-8",
    )
    client.app.state.ai_settings.purge_stored_key()  # type: ignore[attr-defined]
    assert "sk-old" not in config.read_text(encoding="utf-8")


def test_ai_test_endpoint_classifies(client: TestClient, cloud_client: TestClient) -> None:
    empty = client.post("/api/settings/ai/test", json={}, headers=WRITE_HEADERS)
    assert empty.status_code == 200
    assert empty.json() == {"ok": False, "reason": "missing_key"}

    _configure_ai(cloud_client, key=True, consent=True)
    ok = cloud_client.post("/api/settings/ai/test", json={}, headers=WRITE_HEADERS)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "provider": "cloud-fake"}

    _configure_ai(cloud_client, key=True, consent=False)
    gated = cloud_client.post("/api/settings/ai/test", json={}, headers=WRITE_HEADERS)
    assert gated.json() == {"ok": False, "reason": "consent_required"}

    # 选了中国服务但后端尚未接入：要明确报「未接入」，不能误报没选 provider。
    cloud_client.put(
        "/api/settings/ai",
        json={"provider": "dashscope", "allowCloudEdit": True},
        headers=WRITE_HEADERS,
    )
    unintegrated = cloud_client.post("/api/settings/ai/test", json={}, headers=WRITE_HEADERS)
    assert unintegrated.json() == {"ok": False, "reason": "provider_not_integrated"}

    # 表单值可直接参与预检（不必先保存）：把未接入的 provider 放进请求体。
    _configure_ai(cloud_client, key=True, consent=True)
    form_only = cloud_client.post(
        "/api/settings/ai/test",
        json={"provider": "dashscope", "baseUrl": "https://dashscope.aliyuncs.com"},
        headers=WRITE_HEADERS,
    )
    assert form_only.json() == {"ok": False, "reason": "provider_not_integrated"}


def test_probe_form_does_not_reuse_key_across_providers() -> None:
    """换 provider 预检时不能沿用上一家的 Key 与地址。"""
    saved = AiSettings(
        provider="openai-compatible",
        model="gpt-image-1.5",
        api_key="sk-openai",
        base_url="https://api.openai.com/v1",
        allow_cloud_edit=True,
    )
    same = _merge_probe_settings(saved, AiProbeRequest(provider="openai-compatible"))
    assert same.api_key == "sk-openai"
    assert same.base_url == "https://api.openai.com/v1"

    switched = _merge_probe_settings(saved, AiProbeRequest(provider="gemini-image"))
    assert switched.api_key is None
    assert switched.base_url is None
    assert switched.allow_cloud_edit is True
