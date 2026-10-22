"""I14 / I15 AI 线稿预留接口：MVP 不接实现。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.errors import ApiError

router = APIRouter()


@router.get("/lineart/providers")
def list_providers() -> list[dict[str, object]]:
    return []


@router.post("/lineart/generate")
def generate() -> None:
    raise ApiError(501, "E_NOT_IMPLEMENTED", "AI 线稿生成尚未接入")
