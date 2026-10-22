"""I1 存活与版本探测。"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import APP_VERSION

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}
