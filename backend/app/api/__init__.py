"""API 路由聚合。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    routes_edits,
    routes_health,
    routes_lineart,
    routes_plans,
    routes_presets,
    routes_projects,
    routes_tasks,
    routes_windows,
)

api_router = APIRouter(prefix="/api")
for module in (
    routes_health,
    routes_windows,
    routes_projects,
    routes_plans,
    routes_tasks,
    routes_presets,
    routes_lineart,
    routes_edits,
):
    api_router.include_router(module.router)

__all__ = ["api_router"]
