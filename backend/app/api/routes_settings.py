"""I20 AI 配置读写、I21 配置预检。Key 只回掩码，永不回传明文。"""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request

from app.api.schemas import AiProbeRequest, AiSettingsUpdate
from app.services.ai_providers import _is_local_url
from app.services.editing import (
    EditService,
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderTimeoutError,
    ProviderUnavailable,
)
from app.services.settings_store import KEY_HEADER, AiSettings, mask_key

router = APIRouter()


def _header_key(request: Request) -> str:
    return request.headers.get(KEY_HEADER, "").strip()


def _service(request: Request) -> EditService:
    return request.app.state.editor


def _payload(settings: AiSettings) -> dict[str, object]:
    return {
        "provider": settings.provider,
        "model": settings.model,
        "baseUrl": settings.base_url,
        "apiKeyMasked": mask_key(settings.api_key),
        "hasKey": bool(settings.api_key),
        "keyFromEnv": settings.key_from_env,
        "allowCloudEdit": settings.allow_cloud_edit,
        # 环境变量注入的 Key 无法从接口清除，前端据此禁用「清除」按钮。
        "envOverridesKey": settings.key_from_env,
    }


@router.get("/settings/ai")
def get_ai_settings(request: Request) -> dict[str, object]:
    store = _service(request).settings
    return _payload(store.load() if store else AiSettings())


@router.put("/settings/ai")
def put_ai_settings(payload: AiSettingsUpdate, request: Request) -> dict[str, object]:
    store = _service(request).settings
    if store is None:
        return _payload(AiSettings())
    saved = store.save(
        provider=payload.provider,
        model=payload.model,
        # Key 由浏览器持有；这里恒传 None = 不修改文件里的 Key（迁移后本应为空）。
        api_key=None,
        base_url=payload.base_url,
        allow_cloud_edit=payload.allow_cloud_edit,
    )
    return _payload(saved)


def _merge_probe_settings(saved: AiSettings, form: AiProbeRequest) -> AiSettings:
    """把表单值叠到已保存配置上。

    Key 与地址是「每个 provider 各自」的：表单换了 provider 时不能沿用旧值，
    否则会用 A 家的 Key 去预检 B 家，把「没填 Key」误报成鉴权失败。
    """
    provider = (form.provider or "").strip() or None
    switched = provider is not None and provider != saved.provider

    def pick(value: str | None, stored: str | None) -> str | None:
        if value is not None:
            return value.strip() or None
        return None if switched else stored

    return AiSettings(
        provider=provider if provider is not None else saved.provider,
        model=form.model or saved.model,
        api_key=pick(form.api_key, saved.api_key),
        base_url=pick(form.base_url, saved.base_url),
        allow_cloud_edit=(
            form.allow_cloud_edit if form.allow_cloud_edit is not None else saved.allow_cloud_edit
        ),
    )


@router.post("/settings/ai/test")
def test_ai_settings(
    request: Request, payload: AiProbeRequest | None = None
) -> dict[str, object]:
    """配置预检：向所选 provider 发一次不计费的连通性检查。

    可携带表单当前值（provider/model/apiKey/baseUrl），无需先保存；缺省字段
    沿用已保存配置。只回成功/失败分类，不回原始报文。
    """
    service = _service(request)
    store = service.settings
    saved = store.load() if store else AiSettings()
    form = payload or AiProbeRequest()
    header_key = _header_key(request)
    if header_key:
        # 浏览器带来的 Key 优先，且绝不与已保存值混用。
        ai = replace(_merge_probe_settings(saved, form), api_key=header_key)
    else:
        ai = _merge_probe_settings(saved, form)
    generative = [p for p in service.providers() if p.supports in ("generative", "both")]
    if not generative:
        return {"ok": False, "reason": "no_provider"}
    # 先判断所选服务是否已接入：未接入时直接报该原因，不要被「没填 Key」掩盖。
    if ai.provider:
        provider = next((p for p in generative if p.id == ai.provider), None)
        if provider is None:
            return {"ok": False, "reason": "provider_not_integrated"}
    else:
        provider = next(
            (p for p in generative if p.id == "openai-compatible" and ai.base_url), generative[0]
        )
    # 本地部署（baseUrl 指向本机）免 Key，不能因为没 Key 就拒绝预检。
    if not ai.api_key and not ai.base_url:
        return {"ok": False, "reason": "missing_key"}
    if not ai.provider and not ai.base_url:
        return {"ok": False, "reason": "missing_provider"}
    check = getattr(provider, "available", None)
    if callable(check):
        usable = bool(check(ai))
    else:
        usable = bool(ai.api_key) if getattr(provider, "needs_key", False) else True
    if not usable:
        return {
            "ok": False,
            "reason": "missing_key" if getattr(provider, "needs_key", False) else "missing_provider",
        }
    # OpenAI 兼容通道按表单里的地址判定云/本地；其余 provider 用自己的 cloud 标记。
    if provider.id == "openai-compatible":
        is_cloud = bool(ai.base_url) and not _is_local_url(ai.base_url)
    else:
        is_cloud = bool(getattr(provider, "cloud", False))
    if is_cloud and not ai.allow_cloud_edit:
        return {"ok": False, "reason": "consent_required"}
    try:
        probe = getattr(provider, "probe", None)
        if callable(probe):
            probe(ai)
    except ProviderAuthError:
        return {"ok": False, "reason": "auth"}
    except ProviderTimeoutError:
        return {"ok": False, "reason": "timeout"}
    except ProviderUnavailable:
        return {"ok": False, "reason": "unreachable"}
    except ProviderNotConfigured:
        return {"ok": False, "reason": "missing_key"}
    except Exception:  # 第三方 provider 的意外异常不把原始报文带回前端
        return {"ok": False, "reason": "unreachable"}
    return {"ok": True, "provider": provider.id}
