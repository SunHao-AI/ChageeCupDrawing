"""I3 创建项目并上传原图、I4 画布校准，以及原图回读。"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.errors import ApiError
from app.api.schemas import CalibrationRequest
from app.config import MAX_IMAGE_LONG_SIDE, MAX_UPLOAD_BYTES
from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect
from app.store.assets import ImageTooLarge, UnsupportedMedia, save_source_image
from app.store.models import utc_now

router = APIRouter()

_CHUNK = 1024 * 1024


@router.post("/projects", status_code=201)
async def create_project(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> dict[str, object]:
    store = request.app.state.store
    if name is not None and len(name.strip()) > 64:
        raise ApiError(400, "E_INVALID_ARGUMENT", "项目名不能超过 64 个字符")

    project_id = store.new_project_id()
    tmp_path = store.tmp_dir / f"{project_id}.upload"
    size = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ApiError(413, "E_PAYLOAD_TOO_LARGE", "图片超过 20 MB 上限")
                out.write(chunk)
        if size == 0:
            raise ApiError(400, "E_INVALID_ARGUMENT", "上传内容为空")

        try:
            saved = save_source_image(tmp_path, store.project_dir(project_id))
        except UnsupportedMedia as exc:
            raise ApiError(415, "E_UNSUPPORTED_MEDIA", "仅支持 PNG 或 JPEG 图片") from exc
        except ImageTooLarge as exc:
            raise ApiError(
                400,
                "E_INVALID_ARGUMENT",
                f"图片长边不能超过 {MAX_IMAGE_LONG_SIDE} px",
            ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        await file.close()

    fallback = Path(file.filename or "").stem[:64] or "未命名项目"
    project = store.create_project(
        project_id=project_id,
        name=(name.strip() if name and name.strip() else fallback),
        image_path=saved.path,
        image_width=saved.width,
        image_height=saved.height,
    )
    return {
        "projectId": project.id,
        "name": project.name,
        "imageUrl": f"/api/projects/{project.id}/image",
        "width": project.image_width,
        "height": project.image_height,
        "sourceVersion": project.source_version,
        "createdAt": project.created_at,
    }


@router.get("/projects/{project_id}/image")
def project_image(project_id: str, request: Request) -> FileResponse:
    project = request.app.state.store.get_project(project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")
    path = Path(project.image_path)
    if not path.is_file():
        raise ApiError(404, "E_NOT_FOUND", "原图文件已丢失")
    return FileResponse(path)


@router.post("/projects/{project_id}/calibration")
def calibrate(project_id: str, payload: CalibrationRequest, request: Request) -> dict[str, object]:
    store = request.app.state.store
    device = request.app.state.device

    project = store.get_project(project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")
    if not device.alive(payload.hwnd):
        raise ApiError(409, "E_WINDOW_LOST", "目标窗口已关闭")

    # 截图 API 要求目标在前台（被遮挡时截到的是遮挡内容），先替用户把窗口带到
    # 前台；给刚恢复/刚前置的窗口一点重绘时间再截图。
    device.activate(payload.hwnd)
    time.sleep(0.3)

    try:
        capture = device.capture(payload.hwnd, payload.capture_method)
    except CaptureError as exc:
        raise ApiError(422, "E_CAPTURE_FAILED", "窗口截图失败", str(exc)) from exc

    image = capture.image
    if image is None or getattr(image, "size", 0) == 0:
        raise ApiError(422, "E_CAPTURE_FAILED", "窗口截图为空")
    image_height, image_width = int(image.shape[0]), int(image.shape[1])
    dpi_scale = device.dpi_scale(payload.hwnd)

    if payload.mode == "manual":
        if payload.rect is None:
            raise ApiError(400, "E_INVALID_ARGUMENT", "手动校准必须提供 rect")
        if payload.rect.x + payload.rect.w > image_width or payload.rect.y + payload.rect.h > image_height:
            raise ApiError(400, "E_INVALID_ARGUMENT", "rect 超出截图范围")
        rect = Rect(payload.rect.x, payload.rect.y, payload.rect.w, payload.rect.h)
        confidence = 1.0
    else:
        found = detect_canvas_rect(image)
        if found is None:
            return {
                "calibrationId": None,
                "rect": None,
                "imageWidth": image_width,
                "imageHeight": image_height,
                "dpiScale": dpi_scale,
                "confidence": 0.0,
                "captureMethod": capture.method,
                "detectedAt": utc_now(),
            }
        left, top, right, bottom = found
        rect = Rect(left, top, right - left, bottom - top)
        confidence = 1.0

    profile = store.save_calibration(
        project_id=project_id,
        hwnd=payload.hwnd,
        window_title=device.title(payload.hwnd),
        capture_method=capture.method,
        rect_x=rect.x,
        rect_y=rect.y,
        rect_w=rect.w,
        rect_h=rect.h,
        dpi_scale=dpi_scale,
        source=payload.mode,
        confidence=confidence,
        slider=payload.slider.model_dump() if payload.slider else None,
        undo=payload.undo.model_dump() if payload.undo else None,
        fold=payload.fold.model_dump() if payload.fold else None,
        device_kind=payload.device_kind,
        device_serial=payload.device_serial,
    )
    return {
        "calibrationId": profile.id,
        "rect": rect.as_dict(),
        "imageWidth": image_width,
        "imageHeight": image_height,
        "dpiScale": profile.dpi_scale,
        "confidence": profile.confidence,
        "captureMethod": profile.capture_method,
        "detectedAt": profile.captured_at,
        "deviceKind": profile.device_kind,
        "deviceSerial": profile.device_serial,
        "slider": profile.slider,
        "undo": profile.undo,
        "fold": profile.fold,
    }
