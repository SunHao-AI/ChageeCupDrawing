"""AI provider 设置存储：data/config.json + 环境变量覆盖 + Key 掩码。

Key 由浏览器持有并经请求头单次携带（BYOK），服务端不落盘、不回显；
环境变量 CUPDRAW_AI_KEY 仍可用于无人值守部署，优先级最高。
对外读取接口一律只回掩码，永不回传明文。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Literal

EditChannel = Literal["auto", "semantic", "generative"]

_CONFIG_NAME = "config.json"

# 环境变量覆盖：优先级高于 config.json，值不回显到接口。
_ENV_KEY = "CUPDRAW_AI_KEY"
_ENV_PROVIDER = "CUPDRAW_AI_PROVIDER"
_ENV_MODEL = "CUPDRAW_AI_MODEL"
_ENV_BASE_URL = "CUPDRAW_AI_BASE_URL"

# 单次请求内由 X-AI-Key 注入的 Key；请求结束即失效，不进任何持久化路径。
KEY_HEADER = "X-AI-Key"
_REQUEST_KEY: ContextVar[str | None] = ContextVar("cupdraw_request_key", default=None)


@contextmanager
def request_key(key: str | None) -> Iterator[None]:
    token = _REQUEST_KEY.set(key.strip() or None if key else None)
    try:
        yield
    finally:
        _REQUEST_KEY.reset(token)

# gemini-2.5-flash-image 已于 2026-10-02 关停，默认用其继任者 Nano Banana 2。
DEFAULT_MODEL = "gemini-3.1-flash-image"


@dataclass(frozen=True)
class AiSettings:
    provider: str | None = None
    model: str = DEFAULT_MODEL
    api_key: str = ""
    # OpenAI 兼容通道的服务地址；指向本机（Ollama/vLLM 等）时不出网、免 Key。
    base_url: str = ""
    allow_cloud_edit: bool = False
    # 环境变量提供了 Key/Provider 时忽略文件里的对应字段，接口只回标记。
    key_from_env: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.provider)


def mask_key(api_key: str) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}…{api_key[-4:]}"


class AiSettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _CONFIG_NAME

    def load(self) -> AiSettings:
        settings = self._load_file()
        env_key = os.environ.get(_ENV_KEY, "").strip()
        if env_key:
            settings = replace(settings, api_key=env_key, key_from_env=True)
        elif _REQUEST_KEY.get():
            # 浏览器随请求带来的 Key：只在本次调用内生效。
            settings = replace(settings, api_key=_REQUEST_KEY.get() or "", key_from_env=False)
        env_provider = os.environ.get(_ENV_PROVIDER, "").strip()
        if env_provider:
            settings = replace(settings, provider=env_provider)
        env_model = os.environ.get(_ENV_MODEL, "").strip()
        if env_model:
            settings = replace(settings, model=env_model)
        env_base = os.environ.get(_ENV_BASE_URL, "").strip()
        if env_base:
            settings = replace(settings, base_url=env_base)
        return settings

    def save(
        self,
        *,
        provider: str | None,
        model: str | None,
        api_key: str | None,
        base_url: str | None = None,
        allow_cloud_edit: bool,
    ) -> AiSettings:
        current = self._load_file()
        merged = AiSettings(
            provider=(provider.strip() or None) if provider is not None else current.provider,
            model=(model.strip() or DEFAULT_MODEL) if model is not None else current.model,
            # None 表示保持已存的 Key 不动（前端只在用户主动修改时提交明文）。
            api_key=api_key.strip() if api_key is not None else current.api_key,
            base_url=base_url.strip() if base_url is not None else current.base_url,
            allow_cloud_edit=allow_cloud_edit,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "provider": merged.provider,
            "model": merged.model,
            "apiKey": merged.api_key,
            "baseUrl": merged.base_url,
            "allowCloudEdit": merged.allow_cloud_edit,
        }
        # Key 会落盘，先写临时文件再原子替换，避免半截 JSON。
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        return self.load()

    def purge_stored_key(self) -> bool:
        """删除配置文件中残留的 Key（Key 改由浏览器持有，服务端不落盘）。

        幂等：没有 Key 时不动文件。返回是否真的删除过。
        """
        current = self._load_file()
        if not current.api_key:
            return False
        self.save(
            provider=current.provider,
            model=current.model,
            api_key="",
            base_url=current.base_url,
            allow_cloud_edit=current.allow_cloud_edit,
        )
        return True

    def _load_file(self) -> AiSettings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AiSettings()
        if not isinstance(raw, dict):
            return AiSettings()
        provider = raw.get("provider")
        return AiSettings(
            provider=provider.strip() if isinstance(provider, str) and provider.strip() else None,
            model=(raw.get("model") or DEFAULT_MODEL) if isinstance(raw.get("model"), str) else DEFAULT_MODEL,
            api_key=raw.get("apiKey") if isinstance(raw.get("apiKey"), str) else "",
            base_url=raw.get("baseUrl", "").strip() if isinstance(raw.get("baseUrl"), str) else "",
            allow_cloud_edit=bool(raw.get("allowCloudEdit", False)),
        )
