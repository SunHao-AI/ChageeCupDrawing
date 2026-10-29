"""I3 创建项目并上传原图、I4 画布校准，以及原图回读。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.errors import ApiError
from app.api.routes_plans import _summary as _plan_summary
from app.api.routes_tasks import SSE_HEARTBEAT_S, SSE_POLL_S
from app.api.schemas import AutoCalibrationRequest, CalibrationRequest, ClearCanvasRequest
from app.config import MAX_IMAGE_LONG_SIDE, MAX_UPLOAD_BYTES
from app.core.vision.canvas_detect import detect_canvas_rect
from app.device.capture import CaptureError
from app.device.geometry import Rect
from app.executor.service import TaskError
from app.store.assets import ImageTooLarge, UnsupportedMedia, save_source_image
from app.store.models import utc_now

router = APIRouter()

_CHUNK = 1024 * 1024


def _copy_calibration(store, source_project_id: str | None, target_project_id: str) -> None:
    """换图沿用校准（clear-and-guide-design §5）：整体搬运来源项目最新校准。

    坐标天然同帧——两次导入之间窗口未动就是零成本；动过了由落笔前漂移校验
    拦截，或用户在校准页重新自动识别覆盖。来源无效/无校准时静默跳过。
    """
    if not source_project_id or source_project_id == target_project_id:
        return
    source = store.latest_calibration(source_project_id)
    if source is None:
        return
    store.save_calibration(
        project_id=target_project_id,
        hwnd=source.hwnd,
        window_title=source.window_title,
        capture_method=source.capture_method,
        rect_x=source.rect_x,
        rect_y=source.rect_y,
        rect_w=source.rect_w,
        rect_h=source.rect_h,
        dpi_scale=source.dpi_scale,
        source="copied",
        confidence=source.confidence,
        slider=source.slider,
        undo=source.undo,
        fold=source.fold,
        tools_open=source.tools_open,
        tools_collapsed=source.tools_collapsed,
        device_kind=source.device_kind,
        device_serial=source.device_serial,
        blank_baseline=source.blank_baseline,
        frame_w=source.frame_w,
        frame_h=source.frame_h,
    )


@router.post("/projects", status_code=201)
async def create_project(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    copy_calibration_from: str | None = Form(default=None, alias="copyCalibrationFrom"),
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
    _copy_calibration(store, copy_calibration_from, project_id)
    return {
        "projectId": project.id,
        "name": project.name,
        "imageUrl": f"/api/projects/{project.id}/image",
        "width": project.image_width,
        "height": project.image_height,
        "sourceVersion": project.source_version,
        "createdAt": project.created_at,
    }


@router.get("/projects/{project_id}/resume-state")
def project_resume_state(project_id: str, request: Request) -> dict[str, object]:
    """刷新/重开后恢复项目上下文：校准、最新规划与最新任务，前端据此续画。"""
    store = request.app.state.store
    project = store.get_project(project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")

    calibration = store.latest_calibration(project_id)
    plan = (store.list_plans(project_id) or [None])[0]
    task = store.latest_task_for_project(project_id)
    # totalPaths 必须按任务自己的规划算（任务可能属于比 plan 更早的规划）。
    total = 0
    if task is not None:
        task_plan = plan if plan is not None and task.plan_id == plan.id else store.get_plan(task.plan_id)
        total = int(task_plan.metrics.get("pathCount", 0)) if task_plan else 0
    return {
        "project": {
            "projectId": project.id,
            "name": project.name,
            "imageUrl": f"/api/projects/{project.id}/image",
            "width": project.image_width,
            "height": project.image_height,
            "sourceVersion": project.source_version,
            "createdAt": project.created_at,
        },
        "calibration": _calibration_dict(calibration) if calibration else None,
        "plan": _plan_resume_dict(plan),
        "task": {
            "taskId": task.id,
            "planId": task.plan_id,
            "status": task.status,
            "cursorPath": task.cursor_path,
            "totalPaths": total,
            "exec": task.exec_params,
            "stopReason": task.stop_reason,
            "errorCode": task.error_code,
            "startedAt": task.started_at,
            "finishedAt": task.finished_at,
        }
        if task
        else None,
    }


def _calibration_dict(profile) -> dict[str, object]:
    return {
        "calibrationId": profile.id,
        "rect": {"x": profile.rect_x, "y": profile.rect_y, "w": profile.rect_w, "h": profile.rect_h},
        # 校准帧尺寸：前端标注层据此对齐坐标系；旧记录未存时为 0，前端回退原图 naturalSize。
        "imageWidth": profile.frame_w,
        "imageHeight": profile.frame_h,
        "dpiScale": profile.dpi_scale,
        "confidence": profile.confidence,
        "captureMethod": profile.capture_method,
        "detectedAt": profile.captured_at,
        "deviceKind": profile.device_kind,
        "deviceSerial": profile.device_serial,
        "source": profile.source,
        "slider": profile.slider,
        "undo": profile.undo,
        "fold": profile.fold,
        "toolsOpen": profile.tools_open,
        "toolsCollapsed": profile.tools_collapsed,
        "snapshotId": profile.snapshot_id,
    }


def _plan_resume_dict(plan) -> dict[str, object] | None:
    if plan is None:
        return None
    return {**_plan_summary(plan), "params": plan.params}


@router.post("/projects/{project_id}/clear-canvas", status_code=200)
def clear_canvas(
    project_id: str, request: Request, payload: ClearCanvasRequest | None = None
) -> dict[str, object]:
    """I19 清空画布：连点目标画板自己的撤销按钮直到画布空白（同步执行）。"""
    try:
        return request.app.state.executor.clear_canvas(
            project_id,
            confirm_dirty_probe=bool(payload and payload.confirm_dirty_probe),
        )
    except TaskError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc


@router.post("/projects/{project_id}/calibration/auto", status_code=200)
def auto_calibrate(
    project_id: str, request: Request, payload: AutoCalibrationRequest | None = None
) -> dict[str, object]:
    """I20 一键自动校准：画布识别 + 工具条行为探测（含收起态重扫），同步执行。

    过程步骤经 GET calibration/events 拉取；结果快照经 /snapshot 回读。
    """
    body = payload or AutoCalibrationRequest()
    try:
        return request.app.state.executor.auto_calibrate(
            project_id,
            hwnd=body.hwnd,
            confirm_dirty_probe=body.confirm_dirty_probe,
            targets=body.targets,
        )
    except TaskError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc


@router.post("/projects/{project_id}/calibration/auto/stop", status_code=202)
def auto_calibrate_stop(project_id: str, request: Request) -> dict[str, object]:
    """请求中止进行中的一键自动校准（探测线程在下一检查点落 E_STOPPED）。"""
    request.app.state.executor.calibration_stop(project_id)
    return {"projectId": project_id, "stopping": True}


@router.get("/projects/{project_id}/calibration/events")
async def calibration_events(project_id: str, request: Request) -> StreamingResponse:
    """一键自动校准的 SSE 步骤流：游标语义与 task events 一致（Last-Event-ID）。"""
    if request.app.state.store.get_project(project_id) is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")

    last_header = request.headers.get("last-event-id", "0")
    try:
        after_seq = int(last_header)
    except ValueError:
        after_seq = 0

    executor = request.app.state.executor

    async def stream() -> AsyncIterator[str]:
        yield "retry: 1000\n\n"
        cursor = after_seq
        idle = 0.0
        started = time.monotonic()
        while True:
            events, running = executor.calibration_events(project_id, cursor)
            for event in events:
                cursor = event["seq"]
                idle = 0.0
                yield (
                    f"id: {event['seq']}\n"
                    f"event: {event['type']}\n"
                    f"data: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
                )
            if not running:
                # 前端通常先发 POST 再连流；反向顺序时 run 还没落槽，给一段
                # 宽限期轮询而不是立刻断流，否则头几个步骤事件会丢。
                if cursor > after_seq or time.monotonic() - started > 15.0:
                    return
                await asyncio.sleep(SSE_POLL_S)
                continue
            await asyncio.sleep(SSE_POLL_S)
            idle += SSE_POLL_S
            if idle >= SSE_HEARTBEAT_S:
                idle = 0.0
                yield ": ping\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/projects/{project_id}/calibration/snapshot")
def calibration_snapshot(project_id: str, request: Request, snapshot_id: str = "") -> FileResponse:
    """校准快照回读：校准页标注层锚定产出当前校准的那一帧截图。"""
    store = request.app.state.store
    calibration = store.latest_calibration(project_id)
    if calibration is None or not calibration.snapshot_id:
        raise ApiError(404, "E_NOT_FOUND", "尚无校准快照")
    if snapshot_id and snapshot_id != calibration.snapshot_id:
        raise ApiError(404, "E_NOT_FOUND", "快照已过期")
    path = store.snapshot_path(project_id, calibration.snapshot_id)
    if not path.is_file():
        raise ApiError(404, "E_NOT_FOUND", "快照文件已丢失")
    return FileResponse(path, media_type="image/jpeg")


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
        # 小程序重启后句柄会变：按上一份校准的窗口标题找回同名唯一存活窗口，
        # 自动重绑，免得用户对着旧截图反复校准失败。
        rebound = None
        previous = store.latest_calibration(project_id)
        if previous is not None and previous.window_title:
            from app.device.target import find_live_hwnd_by_title

            rebound = find_live_hwnd_by_title(device, previous.window_title)
        if rebound is None:
            raise ApiError(409, "E_WINDOW_LOST", "目标窗口已关闭")
        payload.hwnd = rebound

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

    tools_open = payload.tools_open
    tools_collapsed = payload.tools_collapsed
    if tools_open is None and tools_collapsed is None:
        # 人工兜底重标（校准页 POST 不带 tools 字段）不应丢弃自动探测成果，
        # 否则执行器会回退 legacy 路径并跳过画笔校验。显式传入任一字段时以
        # 请求为准（显式 null 与未传在 Pydantic 中同为 None，故只在两者皆
        # None 时才继承上一份校准的位图）。
        previous = store.latest_calibration(project_id)
        if previous is not None:
            tools_open = previous.tools_open
            tools_collapsed = previous.tools_collapsed

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
        tools_open=tools_open,
        tools_collapsed=tools_collapsed,
        device_kind=payload.device_kind,
        device_serial=payload.device_serial,
        frame_w=image_width,
        frame_h=image_height,
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
        "toolsOpen": profile.tools_open,
        "toolsCollapsed": profile.tools_collapsed,
    }
