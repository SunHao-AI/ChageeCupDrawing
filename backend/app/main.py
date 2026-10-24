"""应用装配：依赖注入、会话校验、错误体与静态资源托管。"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.api.errors import error_response, install_error_handlers
from app.config import (
    APP_VERSION,
    SESSION_COOKIE,
    SESSION_HEADER,
    SESSION_HEADER_VALUE,
    Settings,
)
from app.device.geometry import make_dpi_aware
from app.device.mirror import MirrorService
from app.device.service import DeviceService
from app.executor.service import ExecutorService
from app.services.editing import EditService
from app.services.planning import PlanningService
from app.services.settings_store import AiSettingsStore
from app.store.store import Store

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    settings: Settings | None = None,
    *,
    device: DeviceService | None = None,
    planner: PlanningService | None = None,
    executor_factory: Callable[[Store, DeviceService], ExecutorService] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if settings.session_token is None:
        settings.session_token = secrets.token_urlsafe(32)

    store = Store(settings.data_dir)
    store.init()
    planning = planner or PlanningService(settings.plan_timeout_s)
    devices = device or DeviceService()
    drawing = (executor_factory or ExecutorService)(store, devices)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        make_dpi_aware()
        try:
            yield
        finally:
            planning.close()

    app = FastAPI(title="ChageeCupDrawing", version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.planner = planning
    app.state.device = devices
    app.state.mirror = MirrorService(settings.data_dir, devices)
    app.state.executor = drawing
    app.state.ai_settings = AiSettingsStore(settings.data_dir)
    # Key 由浏览器持有；启动时清掉旧版本落在磁盘上的明文。
    app.state.ai_settings.purge_stored_key()
    app.state.editor = EditService(settings=app.state.ai_settings)
    install_error_handlers(app)

    @app.middleware("http")
    async def session_gate(request: Request, call_next) -> Response:
        token = request.app.state.settings.session_token
        given = request.cookies.get(SESSION_COOKIE)

        if request.method in ("GET", "HEAD", "OPTIONS"):
            response = await call_next(request)
            if given != token:
                response.set_cookie(
                    SESSION_COOKIE,
                    token,
                    httponly=True,
                    samesite="strict",
                    path="/",
                    # 经反向代理时以 X-Forwarded-Proto 判断，避免 HTTPS 下漏发 Secure。
                    secure=request.headers.get(
                        "x-forwarded-proto", request.url.scheme
                    ) == "https",
                )
            return response

        # 写请求必须同时带会话 Cookie 与自定义头，挡住跨站表单提交。
        if given != token or request.headers.get(SESSION_HEADER, "") != SESSION_HEADER_VALUE:
            return error_response(401, "E_SESSION_TOKEN", "会话令牌缺失或无效，请刷新页面")
        return await call_next(request)

    app.include_router(api_router)

    static_dir = settings.static_dir or FRONTEND_DIST
    if Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()
