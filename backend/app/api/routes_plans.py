"""I5 生成规划、I6 读取规划、I7 预览资产。"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.api.errors import ApiError
from app.api.schemas import PlanRequest
from app.config import MAX_PROJECT_ASSET_BYTES
from app.services.planning import PlanFailed, PlanTimeout
from app.store.models import PlanRecord
from app.store.store import ASSET_FILES

router = APIRouter()

_METRIC_KEYS = ("precision", "recall", "iou", "pathCount", "totalLength")


def _summary(record: PlanRecord) -> dict[str, object]:
    return {
        "planId": record.id,
        "status": "DONE",
        "strategy": record.strategy,
        "metrics": {key: record.metrics.get(key) for key in _METRIC_KEYS},
        # 墨迹占比：识别「整图被判成墨」这类参数不匹配（如暗调照片 + 全局阈值）。
        "inkRatio": record.metrics.get("inkRatio"),
        # 实际生效的超采样倍率（源图分辨率不足时会自动降档）。
        "workScale": record.metrics.get("workScale"),
        "targetSize": {"w": record.target_w, "h": record.target_h},
        "assets": {
            kind: f"/api/plans/{record.id}/assets/{kind}" for kind in ASSET_FILES
        },
    }


@router.post("/projects/{project_id}/plans")
def create_plan(project_id: str, payload: PlanRequest, request: Request) -> dict[str, object]:
    store = request.app.state.store
    planner = request.app.state.planner

    project = store.get_project(project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")

    calibration = store.latest_calibration(project_id)
    if calibration is None or calibration.rect_w <= 0 or calibration.rect_h <= 0:
        raise ApiError(422, "E_CANVAS_MISSING", "项目尚未完成画布校准")

    if store.project_asset_bytes(project_id) > MAX_PROJECT_ASSET_BYTES:
        raise ApiError(413, "E_PAYLOAD_TOO_LARGE", "项目资产已超过 200 MB，无法继续规划")

    params = payload.params.model_dump(by_alias=True)
    plan_id = store.new_plan_id()
    out_dir = store.plan_dir(plan_id)
    try:
        result = planner.run(
            image_path=Path(project.image_path),
            target_w=calibration.rect_w,
            target_h=calibration.rect_h,
            params=params,
            out_dir=out_dir,
        )
    except PlanTimeout as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ApiError(504, "E_PLAN_TIMEOUT", "规划超时，请降低分辨率或简化参数") from exc
    except PlanFailed as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ApiError(422, "E_PLAN_FAILED", "未能生成可绘制的路径", str(exc)) from exc

    record = store.create_plan(
        plan_id=plan_id,
        project_id=project_id,
        params=params,
        strategy=str(result["strategy"]),
        metrics=dict(result["metrics"]),
        paths_path=store.plan_paths_path(plan_id),
        target_w=calibration.rect_w,
        target_h=calibration.rect_h,
    )
    return _summary(record)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request) -> dict[str, object]:
    record = request.app.state.store.get_plan(plan_id)
    if record is None:
        raise ApiError(404, "E_NOT_FOUND", "规划不存在")
    return {
        **_summary(record),
        "params": record.params,
        "createdAt": record.created_at,
    }


@router.get("/plans/{plan_id}/assets/{kind}")
def get_plan_asset(plan_id: str, kind: str, request: Request) -> FileResponse:
    store = request.app.state.store
    if kind not in ASSET_FILES:
        raise ApiError(404, "E_NOT_FOUND", "未知的预览图类型")
    if store.get_plan(plan_id) is None:
        raise ApiError(404, "E_NOT_FOUND", "规划不存在")
    path = store.plan_asset_path(plan_id, kind)
    if not path.is_file():
        raise ApiError(404, "E_NOT_FOUND", "预览图不存在")
    return FileResponse(path)
