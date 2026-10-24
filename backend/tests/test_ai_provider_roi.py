"""M-B 生成通道测试：ROI 裁—编—回填 + Gemini / OpenAI 兼容 provider（假 HTTP）。

不产生任何真实计费调用：provider 的 httpx 客户端用 MockTransport 注入固定响应。
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.ai_providers import (
    DashScopeProvider,
    GeminiImageProvider,
    OpenAICompatibleProvider,
)
from app.services.editing import (
    BuiltinSemanticProvider,
    CloudConsentRequired,
    EditService,
    ProviderAuthError,
    ProviderTimeoutError,
)
from app.services.roi_pipeline import (
    blend_ink,
    feather_alpha,
    generative_edit,
    letterbox,
    unletterbox,
)
from app.services.settings_store import AiSettingsStore


def _canvas(width: int = 300, height: int = 400) -> Image.Image:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, width - 40, height - 40), outline=0, width=6)
    return image


def _ink(image: Image.Image) -> np.ndarray:
    return (np.asarray(image.convert("L")) < 128).astype(np.uint8)


def _png_b64(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.convert("L").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ioi(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum()) / float(union) if union else 1.0


def _no_new_ink(result: np.ndarray, original: np.ndarray, tolerance: int = 2) -> bool:
    """回填后不应出现原图 2px 邻域之外的墨迹（接缝长线的判据）。"""
    kernel = np.ones((tolerance * 2 + 1, tolerance * 2 + 1), dtype=np.uint8)
    dilated = cv2.dilate(original, kernel)
    return bool(np.logical_and(result, dilated == 0).sum() == 0)


# ---------- ROI 管线 ----------


def test_letterbox_roundtrip_keeps_size_and_content() -> None:
    image = _canvas(200, 100)
    canvas, meta = letterbox(image, edge=256)
    assert canvas.size == (256, 256)
    restored = unletterbox(canvas, meta, image.size)
    assert restored.size == image.size
    assert _ioi(_ink(restored), _ink(image)) >= 0.85
    assert _no_new_ink(_ink(restored), _ink(image))


def test_unchanged_output_preserves_seam(tmp_path: Path) -> None:
    """模型原样返回时，回填不应在接缝处长出新的墨线。"""
    source = tmp_path / "source.png"
    _canvas().save(source)
    out = tmp_path / "draft.png"
    generative_edit(source, None, "保持原样", out, lambda canvas, _prompt: canvas)

    original = _ink(Image.open(source))
    result = _ink(Image.open(out))
    assert _ioi(result, original) >= 0.85
    assert _no_new_ink(result, original)
    # 四周 8px 过渡带内不应凭空出现墨迹。
    border = np.zeros_like(original, dtype=bool)
    border[:8, :] = border[-8:, :] = True
    border[:, :8] = border[:, -8:] = True
    assert int(np.logical_and(result, border).sum()) == 0


def test_roi_edit_only_touches_selection(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    _canvas().save(source)
    out = tmp_path / "draft.png"
    roi = {"x": 100, "y": 150, "w": 100, "h": 100}

    def fill_black(canvas: Image.Image, _prompt: str) -> Image.Image:
        ImageDraw.Draw(canvas).rectangle((0, 0, canvas.width, canvas.height), fill=0)
        return canvas

    generative_edit(source, roi, "涂黑", out, fill_black)
    result = _ink(Image.open(out))
    # 远在选区外的区域保持原样（空白），说明回填受选区约束。
    assert result[5:40, 5:40].sum() == 0
    assert result[380:395, 5:40].sum() == 0
    assert result[200, 150] == 1


def test_feather_alpha_edges_fade() -> None:
    alpha = feather_alpha((64, 64))
    assert alpha[32, 32] == pytest.approx(1.0)
    assert alpha[0, 32] < 0.2
    assert alpha[32, 0] < 0.2
    assert alpha[0, 0] < alpha[0, 32]


def test_blend_ink_outside_region_untouched() -> None:
    base = np.zeros((20, 20), dtype=np.uint8)
    base[2:5, 2:5] = 255
    patch = np.zeros((6, 6), dtype=np.uint8)
    patch[:] = 255
    out = blend_ink(base, patch, (10, 10, 16, 16))
    assert np.array_equal(out[:10, :], base[:10, :])
    assert out[13, 13] == 255


# ---------- provider（假 HTTP） ----------


def _settings(tmp_path: Path, **kwargs: object) -> AiSettingsStore:
    store = AiSettingsStore(tmp_path)
    store.save(
        provider=kwargs.get("provider"),  # type: ignore[arg-type]
        model=kwargs.get("model"),  # type: ignore[arg-type]
        api_key=kwargs.get("api_key", ""),  # type: ignore[arg-type]
        base_url=kwargs.get("base_url"),  # type: ignore[arg-type]
        allow_cloud_edit=bool(kwargs.get("allow_cloud_edit", False)),
    )
    return store


def _gemini_client(data: str, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"inlineData": {"data": data}}]}}]},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gemini_generate_roundtrip(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="gemini-image", api_key="AIza-test-key")
    source = tmp_path / "source.png"
    _canvas().save(source)
    seen: list[httpx.Request] = []
    provider = GeminiImageProvider(store, client=_gemini_client(_png_b64(_canvas()), seen))

    out = provider.generate(source, None, "去掉边框", tmp_path / "draft.png")

    assert Image.open(out).size == (300, 400)
    assert seen and "gemini-3.1-flash-image" in str(seen[0].url)
    assert seen[0].headers["x-goog-api-key"] == "AIza-test-key"
    # 输出与底图一致时墨迹不新增（重采样只允许 ±2px 漂移）。
    source_ink = _ink(Image.open(source))
    assert _no_new_ink(_ink(Image.open(out)), source_ink)
    assert _ioi(_ink(Image.open(out)), source_ink) >= 0.85


def test_gemini_auth_error_classified(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="gemini-image", api_key="bad")
    provider = GeminiImageProvider(
        store, client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403)))
    )
    source = tmp_path / "source.png"
    _canvas().save(source)
    with pytest.raises(ProviderAuthError):
        provider.generate(source, None, "去掉边框", tmp_path / "draft.png")


def _openai_client(data: str):  # noqa: ANN202 - 测试辅助
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"data": [{"b64_json": data}]})
        )
    )


def test_openai_compatible_local_needs_no_key(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="openai-compatible", base_url="http://127.0.0.1:11434/v1")
    provider = OpenAICompatibleProvider(store, client=_openai_client(_png_b64(_canvas())))
    assert provider.cloud is False
    assert provider.needs_key is False
    assert provider.available(store.load()) is True

    source = tmp_path / "source.png"
    _canvas().save(source)
    out = provider.generate(source, {"x": 0, "y": 0, "w": 100, "h": 100}, "加粗", tmp_path / "d.png")
    assert out.is_file()


def test_openai_compatible_cloud_requires_key(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="openai-compatible", base_url="https://api.example.com/v1")
    provider = OpenAICompatibleProvider(store)
    assert provider.cloud is True
    assert provider.needs_key is True
    assert provider.available(store.load()) is False


def test_openai_compatible_timeout_classified(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    store = _settings(
        tmp_path, provider="openai-compatible", base_url="https://api.example.com/v1", api_key="k"
    )
    provider = OpenAICompatibleProvider(store, client=httpx.Client(transport=httpx.MockTransport(handler)))
    source = tmp_path / "source.png"
    _canvas().save(source)
    with pytest.raises(ProviderTimeoutError):
        provider.generate(source, None, "去掉边框", tmp_path / "draft.png")


def test_chat_only_gateway_reports_capability_mismatch(tmp_path: Path) -> None:
    """纯文本/视觉网关没有 /images/edits，报错要说明能力不匹配而不是地址错误。"""
    from app.services.editing import ProviderUnavailable

    store = _settings(
        tmp_path, provider="openai-compatible", base_url="http://36.1.2.3:5000/v1", api_key="k"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                404, json={"error": {"message": "unknown endpoint: /v1/images/edits"}}
            )
        )
    )
    provider = OpenAICompatibleProvider(store, client=client)
    source = tmp_path / "source.png"
    _canvas().save(source)
    with pytest.raises(ProviderUnavailable, match="不支持图片编辑接口"):
        provider.generate(source, None, "加字", tmp_path / "draft.png")


def _probe_client(status: int) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(status)))


def test_gemini_probe_classifies_auth(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="gemini-image", api_key="bad")
    with pytest.raises(ProviderAuthError):
        GeminiImageProvider(store, client=_probe_client(403)).probe()


def test_openai_probe_tolerates_missing_models_endpoint(tmp_path: Path) -> None:
    """本机部署（Ollama/vLLM 部分版本）可能没有 /models，能连通即视为可用。"""
    store = _settings(tmp_path, provider="openai-compatible", base_url="http://127.0.0.1:11434/v1")
    OpenAICompatibleProvider(store, client=_probe_client(404)).probe()


def test_openai_probe_requires_base_url(tmp_path: Path) -> None:
    from app.services.editing import ProviderNotConfigured

    store = _settings(tmp_path, provider="openai-compatible", api_key="k")
    with pytest.raises(ProviderNotConfigured):
        OpenAICompatibleProvider(store, client=_probe_client(200)).probe()


def _dashscope_client(data: str, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("/multimodal-generation/generation"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "choices": [
                            {"message": {"content": [{"image": "https://oss.example.com/out.png"}]}}
                        ]
                    }
                },
            )
        return httpx.Response(200, content=base64.b64decode(data))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_dashscope_generate_roundtrip(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="dashscope", api_key="sk-dashscope")
    source = tmp_path / "source.png"
    _canvas().save(source)
    seen: list[httpx.Request] = []
    provider = DashScopeProvider(store, client=_dashscope_client(_png_b64(_canvas()), seen))

    out = provider.generate(source, {"x": 50, "y": 50, "w": 120, "h": 120}, "加粗", tmp_path / "d.png")

    assert out.is_file()
    assert seen, "应至少发一次提交请求与一次结果图下载"
    submit = seen[0]
    assert submit.url.path.endswith("/multimodal-generation/generation")
    assert submit.headers["Authorization"] == "Bearer sk-dashscope"
    body = json.loads(submit.content)
    assert body["model"] == "qwen-image-2.0"
    assert body["input"]["messages"][0]["content"][0]["image"].startswith("data:image/png;base64,")


def test_dashscope_auth_error_classified(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="dashscope", api_key="bad")
    provider = DashScopeProvider(
        store, client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(401)))
    )
    source = tmp_path / "source.png"
    _canvas().save(source)
    with pytest.raises(ProviderAuthError):
        provider.generate(source, None, "去掉边框", tmp_path / "draft.png")


def test_dashscope_probe_classifies_auth(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="dashscope", api_key="bad")
    with pytest.raises(ProviderAuthError):
        DashScopeProvider(store, client=_probe_client(401)).probe()

    ok_store = _settings(tmp_path / "ok", provider="dashscope", api_key="sk-dashscope")
    DashScopeProvider(ok_store, client=_probe_client(200)).probe()


def test_auto_without_generative_provider_stays_422(tmp_path: Path) -> None:
    """只注册语义 provider 时，规则未命中仍回「未理解」，不误报未配置。"""
    from app.services.editing import EditNotUnderstood

    store = _settings(tmp_path, provider=None)
    service = EditService([BuiltinSemanticProvider()], settings=store)
    with pytest.raises(EditNotUnderstood):
        service.route("把头发改密", None, "auto", None)


def test_default_providers_include_generative(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="gemini-image", api_key="k", allow_cloud_edit=True)
    service = EditService(settings=store)
    ids = {p.id for p in service.providers()}
    assert {"builtin-semantic", "gemini-image", "openai-compatible", "dashscope"} <= ids

    decision = service.route("把头发改密并加字", None, "generative", None)
    assert decision.channel == "generative"
    assert decision.provider.id == "gemini-image"


def test_local_provider_bypasses_consent(tmp_path: Path) -> None:
    store = _settings(tmp_path, provider="openai-compatible", base_url="http://127.0.0.1:11434/v1")
    service = EditService(settings=store)
    decision = service.route("加艺术字", None, "generative", None)
    assert decision.provider.id == "openai-compatible"

    # 云端 provider 未授权时仍然拦下。
    cloud_store = _settings(tmp_path / "cloud", provider="gemini-image", api_key="k")
    with pytest.raises(CloudConsentRequired):
        EditService(settings=cloud_store).route("加艺术字", None, "generative", None)
