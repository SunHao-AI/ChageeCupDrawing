"""统一错误体 ``{errorCode, message, detail?}`` 与异常处理。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """携带 HTTP 状态与业务错误码的显式失败。"""

    def __init__(
        self, status: int, code: str, message: str, detail: object | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def error_response(
    status: int, code: str, message: str, detail: object | None = None
) -> JSONResponse:
    body: dict[str, object] = {"errorCode": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [
            {
                "path": ".".join(str(part) for part in error.get("loc", ())),
                "message": str(error.get("msg", "")),
            }
            for error in exc.errors()
        ]
        return error_response(400, "E_INVALID_ARGUMENT", "请求参数不合法", detail)
