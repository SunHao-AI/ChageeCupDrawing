"""内置生成 provider：Gemini 原生 与 OpenAI 兼容（含本机部署）。

两类 provider 共用 ROI 管线（裁块 → letterbox → 模型编辑 → 羽化回填），差别只在
HTTP 协议。OpenAI 兼容通道的 baseUrl 指向本机（Ollama / vLLM 等）时视为本地
provider：免 Key、也不会被「云上传授权」拦住；指向远端时才按云端处理。

provider 不缓存配置，每次调用都从 AiSettingsStore 读取，设置页保存后立即生效。
HTTP 客户端可注入，测试用 httpx.MockTransport 固定响应，不产生真实计费调用。
"""

from __future__ import annotations

import base64
import io
import ipaddress
import json
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from PIL import Image

from app.services.editing import (
    EditNotUnderstood,
    EditOp,
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderTimeoutError,
    ProviderUnavailable,
)
from app.services.roi_pipeline import MODEL_EDGE, generative_edit
from app.services.settings_store import DEFAULT_MODEL, AiSettings, AiSettingsStore

GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_DEFAULT_MODEL = DEFAULT_MODEL
OPENAI_DEFAULT_MODEL = "gpt-image-1.5"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com"
# 万相 wanx 系列只收公网图片地址，本地草稿无法直接送；千问图像编辑
# 接收 data URL，才是能对本地选区块生效的通义通道。
# 早期模型 qwen-image-edit 已停止服务（报 Model not exist），现行默认走 qwen-image-2.0。
DASHSCOPE_DEFAULT_MODEL = "qwen-image-2.0"
DEFAULT_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


def _model_for(ai: AiSettings, default: str) -> str:
    # 用户没改过模型（仍是 Gemini 的默认值）时用 provider 自己的默认模型。
    if ai.model and ai.model != DEFAULT_MODEL:
        return ai.model
    return default


def _b64_png(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.convert("L").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode_b64_image(data: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data)))


def _is_local_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _api_base(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _error_hint(text: str) -> str:
    """从上游错误体提取可行动的提示（百炼为 {code,message}，OpenAI 兼容为 {error:{message}}）。"""
    try:
        body = json.loads(text)
    except ValueError:
        return text.strip()[:160]
    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message")
        if code or message:
            joined = ": ".join(str(x) for x in (code, message) if x)
            return joined[:160]
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:160]
    return ""


class _HttpProvider:
    """公共错误分类：鉴权、超时、服务不可用，原始报文不外传。"""

    default_model = ""

    def __init__(
        self,
        store: AiSettingsStore | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._store = store
        self._client = client

    # ---------- 设置 ----------

    def _settings(self) -> AiSettings:
        if self._store is None:
            raise ProviderNotConfigured("AI 设置未接入，无法调用生成模型")
        return self._store.load()

    def available(self, ai: AiSettings | None) -> bool:
        raise NotImplementedError

    # ---------- EditProvider 协议 ----------

    def parse(self, instruction: str) -> list[EditOp]:
        raise EditNotUnderstood("生成 provider 不负责语义解析")

    def apply(self, image: Path, ops: list[EditOp], out_path: Path) -> Path:
        raise EditNotUnderstood("生成 provider 不执行语义 apply")

    def generate(
        self, image: Path, roi: dict | None, instruction: str, out_path: Path
    ) -> Path:
        ai = self._settings()
        if not self.available(ai):
            raise ProviderNotConfigured("生成 provider 尚未配置完整，请到设置页填写")
        generative_edit(
            image,
            roi,
            instruction,
            out_path,
            lambda canvas, prompt: self._request_edit(canvas, prompt, ai),
        )
        return out_path

    # ---------- HTTP ----------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        return self._client

    def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._http().request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("生成模型响应超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"无法连接生成模型服务：{exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError("API Key 无效或无权访问该模型")
        if response.status_code == 429:
            raise ProviderAuthError("调用额度不足或触发限流，请稍后重试")
        lowered = response.text.lower()
        if response.status_code == 400 and ("api key" in lowered or "api_key" in lowered):
            raise ProviderAuthError("API Key 无效，请检查设置")
        # 多数 OpenAI 兼容网关（vLLM/Ollama 的纯文本或视觉模型）没有 /images/edits，
        # 笼统报 HTTP 404 会让人以为是地址写错，这里直接点明能力不匹配。
        if response.status_code == 404 and "unknown endpoint" in lowered:
            raise ProviderUnavailable(
                "该服务不支持图片编辑接口（/images/edits），请换用支持图像生成的模型或服务"
            )
        if response.status_code >= 400:
            # 透传上游错误体（截断），否则只剩 HTTP 码无法定位「模型未开通 / 参数不符」等具体原因。
            detail = _error_hint(response.text)
            suffix = f"：{detail}" if detail else ""
            raise ProviderUnavailable(f"生成模型服务返回错误（HTTP {response.status_code}）{suffix}")
        return response

    def _request_edit(self, image: Image.Image, prompt: str, ai: AiSettings) -> Image.Image:
        raise NotImplementedError

    def probe(self, ai: AiSettings | None = None) -> None:
        """连通性预检：只查模型列表，不发计费生成请求。"""
        raise NotImplementedError


class GeminiImageProvider(_HttpProvider):
    """Gemini generateContent：一句话 + 图，最贴合当前「指令 + 选区」交互。"""

    id = "gemini-image"
    label = "Gemini 图像编辑"
    supports = "generative"
    cloud = True
    needs_key = True
    default_model = GEMINI_DEFAULT_MODEL

    def available(self, ai: AiSettings | None) -> bool:
        if ai is None or not ai.api_key:
            return False
        return ai.provider in (None, self.id)

    def _request_edit(self, image: Image.Image, prompt: str, ai: AiSettings) -> Image.Image:
        model = _model_for(ai, self.default_model)
        url = f"{GEMINI_BASE}/v1beta/models/{quote(model, safe='')}:generateContent"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/png", "data": _b64_png(image)}},
                    ]
                }
            ]
        }
        response = self._send(
            "POST", url, json=payload, headers={"x-goog-api-key": ai.api_key}
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable("生成模型返回了无法解析的内容") from exc
        for candidate in body.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return _decode_b64_image(inline["data"])
        raise ProviderUnavailable("生成模型未返回图像，请调整指令后重试")

    def probe(self, ai: AiSettings | None = None) -> None:
        ai = ai or self._settings()
        if not ai.api_key:
            raise ProviderNotConfigured("尚未填写 API Key")
        self._send(
            "GET", f"{GEMINI_BASE}/v1beta/models", headers={"x-goog-api-key": ai.api_key}
        )


class OpenAICompatibleProvider(_HttpProvider):
    """OpenAI 兼容 /images/edits：覆盖 DeepSeek、Qwen 等兼容网关与本机部署。"""

    id = "openai-compatible"
    label = "OpenAI 兼容 / 本地部署"
    supports = "generative"
    default_model = OPENAI_DEFAULT_MODEL

    @property
    def cloud(self) -> bool:
        ai = self._store.load() if self._store else None
        return bool(ai and ai.base_url and not _is_local_url(ai.base_url))

    @property
    def needs_key(self) -> bool:
        return self.cloud

    def available(self, ai: AiSettings | None) -> bool:
        if ai is None or not ai.base_url:
            return False
        if ai.provider not in (None, self.id):
            return False
        return _is_local_url(ai.base_url) or bool(ai.api_key)

    def _headers(self, ai: AiSettings) -> dict[str, str]:
        if ai.api_key:
            return {"Authorization": f"Bearer {ai.api_key}"}
        return {}

    def _request_edit(self, image: Image.Image, prompt: str, ai: AiSettings) -> Image.Image:
        model = _model_for(ai, self.default_model)
        url = f"{_api_base(ai.base_url)}/images/edits"
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        response = self._send(
            "POST",
            url,
            headers=self._headers(ai),
            data={
                "model": model,
                "prompt": prompt,
                "size": f"{MODEL_EDGE}x{MODEL_EDGE}",
                "n": "1",
            },
            files={"image": ("crop.png", buf.getvalue(), "image/png")},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable("生成模型返回了无法解析的内容") from exc
        items = body.get("data") or []
        if items and items[0].get("b64_json"):
            return _decode_b64_image(items[0]["b64_json"])
        raise ProviderUnavailable("生成模型未返回图像，请确认该服务支持图片编辑接口")

    def probe(self, ai: AiSettings | None = None) -> None:
        ai = ai or self._settings()
        if not ai.base_url:
            raise ProviderNotConfigured("尚未填写服务地址")
        url = f"{_api_base(ai.base_url)}/models"
        try:
            response = self._http().get(url, headers=self._headers(ai))
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("服务响应超时，请检查地址与网络") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"无法连接服务：{exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError("API Key 无效或无权访问该服务")
        if response.status_code in (404, 405):
            # 部分本机部署不提供 /models，能连通即视为配置正确。
            return
        if response.status_code >= 400:
            raise ProviderUnavailable(f"服务返回错误（HTTP {response.status_code}）")


class DashScopeProvider(_HttpProvider):
    """阿里云百炼 DashScope：qwen-image-edit 多模态编辑（支持 data URL 直传）。"""

    id = "dashscope"
    label = "通义图像编辑"
    supports = "generative"
    cloud = True
    needs_key = True
    default_model = DASHSCOPE_DEFAULT_MODEL

    def available(self, ai: AiSettings | None) -> bool:
        if ai is None or not ai.api_key:
            return False
        return ai.provider in (None, self.id)

    def _headers(self, ai: AiSettings) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ai.api_key}",
            "Content-Type": "application/json",
        }

    def _request_edit(self, image: Image.Image, prompt: str, ai: AiSettings) -> Image.Image:
        model = _model_for(ai, self.default_model)
        if model.startswith("wanx"):
            raise ProviderUnavailable(
                "wanx 系列只接受公网图片地址，本地草稿请改用 qwen-image-edit 系列"
            )
        url = f"{DASHSCOPE_BASE}/api/v1/services/aigc/multimodal-generation/generation"
        payload = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": f"data:image/png;base64,{_b64_png(image)}"},
                            {"text": prompt},
                        ],
                    }
                ]
            },
            "parameters": {"watermark": False},
        }
        response = self._send("POST", url, json=payload, headers=self._headers(ai))
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable("生成模型返回了无法解析的内容") from exc
        image_url = _dashscope_image_url(body)
        if not image_url:
            raise ProviderUnavailable("生成模型未返回图像，请调整指令后重试")
        downloaded = self._send("GET", image_url)
        try:
            return Image.open(io.BytesIO(downloaded.content))
        except Exception as exc:  # 结果图损坏或不是图片
            raise ProviderUnavailable("生成结果无法解析为图像") from exc

    def probe(self, ai: AiSettings | None = None) -> None:
        ai = ai or self._settings()
        if not ai.api_key:
            raise ProviderNotConfigured("尚未填写 API Key")
        url = f"{DASHSCOPE_BASE}/compatible-mode/v1/models"
        try:
            response = self._http().get(url, headers=self._headers(ai))
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("服务响应超时，请检查地址与网络") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"无法连接服务：{exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError("API Key 无效或无权访问百炼服务")
        if response.status_code in (404, 405):
            # 兼容模式不保证提供 /models，能连通即视为配置正确。
            return
        if response.status_code >= 400:
            raise ProviderUnavailable(f"服务返回错误（HTTP {response.status_code}）")


def _dashscope_image_url(body: dict) -> str | None:
    for choice in (body.get("output") or {}).get("choices") or []:
        for part in (choice.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("image"):
                return str(part["image"])
    return None


def generative_providers(store: AiSettingsStore | None) -> list[_HttpProvider]:
    return [
        GeminiImageProvider(store),
        OpenAICompatibleProvider(store),
        DashScopeProvider(store),
    ]
