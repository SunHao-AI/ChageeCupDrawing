"""I16 编辑 provider 列表、I17 发起编辑、I18 应用草稿、I19 丢弃草稿。"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from PIL import Image

from app.api.errors import ApiError
from app.api.schemas import EditRequest
from app.services.editing import (
    EditNotUnderstood,
    EditService,
    NoGenerativeProvider,
    UnknownProvider,
    load_ink,
)
from app.store.models import EditDraft, Project
from app.store.store import Store

router = APIRouter()

# 应用草稿前的墨量护栏（设计稿 7）：编辑后墨迹占比越界直接阻断。
_APPLY_INK_MIN = 0.01
_APPLY_INK_MAX = 0.40


def _service(request: Request) -> EditService:
    return request.app.state.editor


def _semantic_base(store: Store, project: Project) -> Path:
    """语义通道以最新规划掩膜为基准（设计稿 2.1：结果与预览所见一致）。

    尚无规划时回落到源图，此时编辑语义退化为「编辑二值化前的原图」。
    """
    for plan in store.list_plans(project.id):
        mask = store.plan_asset_path(plan.id, "target")
        if mask.is_file():
            return mask
    return Path(project.image_path)


def _image_size(path: Path) -> tuple[int, int]:
    if not path.is_file():
        raise ApiError(422, "E_CANVAS_MISSING", "编辑基准图不存在，请先生成一次规划")
    with Image.open(path) as img:
        return img.size


def _draft_payload(draft: EditDraft) -> dict[str, object]:
    return {
        "editId": draft.id,
        "projectId": draft.project_id,
        "channel": draft.channel,
        "provider": draft.provider,
        "instruction": draft.instruction,
        "roi": draft.roi,
        "baseVersion": draft.base_version,
        "draftUrl": f"/api/edits/{draft.id}/draft",
        "diffUrl": f"/api/edits/{draft.id}/diff" if draft.diff_path else None,
        "createdAt": draft.created_at,
        "appliedAt": draft.applied_at,
    }


@router.get("/edit/providers")
def list_providers(request: Request) -> list[dict[str, object]]:
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "kind": provider.supports,
            "local": provider.id == "builtin-semantic",
        }
        for provider in _service(request).providers()
    ]


@router.post("/projects/{project_id}/edits", status_code=201)
def create_edit(project_id: str, payload: EditRequest, request: Request) -> dict[str, object]:
    store = request.app.state.store
    project = store.get_project(project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")
    if payload.base_version != project.source_version:
        raise ApiError(
            409,
            "E_VERSION_STALE",
            "项目源图已有更新版本，请基于最新版本重新编辑",
            {"currentVersion": project.source_version},
        )

    edit_id = store.new_edit_id()
    service = _service(request)
    try:
        # 语义通道编辑最新规划掩膜（与预览一致），生成通道编辑源图（设计稿 2.1）。
        try:
            semantic = service.is_semantic(payload.provider)
        except EditNotUnderstood as exc:
            raise ApiError(422, "E_EDIT_NOT_UNDERSTOOD", "没有可用的语义编辑 provider") from exc
        base_image = _semantic_base(store, project) if semantic else Path(project.image_path)

        roi = None
        if payload.roi is not None:
            base_w, base_h = _image_size(base_image)
            rect = payload.roi
            if rect.x + rect.w > base_w or rect.y + rect.h > base_h:
                raise ApiError(400, "E_INVALID_ARGUMENT", "选区超出编辑基准图范围")
            roi = rect.model_dump()

        result = service.create_draft(
            base_image=base_image,
            instruction=payload.instruction.strip(),
            roi=roi,
            provider_id=payload.provider,
            out_dir=store.edit_dir(edit_id),
        )
    except EditNotUnderstood as exc:
        shutil.rmtree(store.edit_dir(edit_id), ignore_errors=True)
        raise ApiError(422, "E_EDIT_NOT_UNDERSTOOD", "没有理解这条指令，请换一种说法", str(exc)) from exc
    except UnknownProvider as exc:
        raise ApiError(400, "E_INVALID_ARGUMENT", str(exc)) from exc
    except NoGenerativeProvider as exc:
        shutil.rmtree(store.edit_dir(edit_id), ignore_errors=True)
        raise ApiError(501, "E_NOT_IMPLEMENTED", str(exc)) from exc
    except ApiError:
        shutil.rmtree(store.edit_dir(edit_id), ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(store.edit_dir(edit_id), ignore_errors=True)
        raise ApiError(422, "E_EDIT_FAILED", "编辑执行失败", str(exc)) from exc

    draft = store.create_edit_draft(
        edit_id=edit_id,
        project_id=project_id,
        base_version=project.source_version,
        channel=result.channel,
        instruction=payload.instruction.strip(),
        roi=roi,
        provider=result.provider_id,
        draft_path=result.draft_path,
        diff_path=result.diff_path,
    )
    return {
        **_draft_payload(draft),
        "parsedOps": [op.as_dict() for op in result.parsed_ops] if result.parsed_ops else None,
    }


def _editable_draft(store: object, edit_id: str) -> EditDraft:
    draft = store.get_edit_draft(edit_id)  # type: ignore[attr-defined]
    if draft is None:
        raise ApiError(404, "E_NOT_FOUND", "编辑草稿不存在")
    return draft


@router.get("/edits/{edit_id}/draft")
def get_draft_image(edit_id: str, request: Request) -> FileResponse:
    draft = _editable_draft(request.app.state.store, edit_id)
    path = Path(draft.draft_path)
    if not path.is_file():
        raise ApiError(404, "E_NOT_FOUND", "草稿图片已丢失")
    return FileResponse(path)


@router.get("/edits/{edit_id}/diff")
def get_diff_image(edit_id: str, request: Request) -> FileResponse:
    draft = _editable_draft(request.app.state.store, edit_id)
    if not draft.diff_path:
        raise ApiError(404, "E_NOT_FOUND", "该编辑没有差异图")
    path = Path(draft.diff_path)
    if not path.is_file():
        raise ApiError(404, "E_NOT_FOUND", "差异图已丢失")
    return FileResponse(path)


@router.post("/edits/{edit_id}/apply")
def apply_edit(edit_id: str, request: Request) -> dict[str, object]:
    store = request.app.state.store
    draft = _editable_draft(store, edit_id)
    if draft.applied_at is not None:
        raise ApiError(409, "E_EDIT_APPLIED", "该草稿已应用")

    project = store.get_project(draft.project_id)
    if project is None:
        raise ApiError(404, "E_NOT_FOUND", "项目不存在")
    if draft.base_version != project.source_version:
        raise ApiError(409, "E_VERSION_STALE", "草稿基于过期版本，请重新编辑")

    draft_path = Path(draft.draft_path)
    if not draft_path.is_file():
        raise ApiError(409, "E_NOT_FOUND", "草稿图片已丢失")

    # 墨量护栏：编辑产物过空或过满都不允许进入绘制链路。
    ratio = float((load_ink(draft_path) > 0).mean())
    if ratio < _APPLY_INK_MIN or ratio > _APPLY_INK_MAX:
        raise ApiError(
            422,
            "E_EDIT_INK_OUT_OF_RANGE",
            f"编辑后墨量占比 {ratio:.1%} 超出安全范围，请调整指令或放弃本次编辑",
            {"inkRatio": ratio},
        )

    new_version = project.source_version + 1
    new_path = store.project_dir(project.id) / f"source_v{new_version}.png"
    try:
        shutil.copyfile(draft_path, new_path)
        with Image.open(new_path) as img:
            width, height = img.size

        applied = store.apply_edit_draft(
            edit_id=edit_id,
            new_image_path=new_path,
            image_width=width,
            image_height=height,
        )
    except ApiError:
        new_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        new_path.unlink(missing_ok=True)
        raise ApiError(422, "E_EDIT_FAILED", "应用编辑失败，原版本不受影响", str(exc)) from exc

    if applied is None:
        new_path.unlink(missing_ok=True)
        raise ApiError(409, "E_VERSION_STALE", "草稿状态已变化，请刷新后重试")

    updated_project, updated_draft = applied
    return {
        "projectId": updated_project.id,
        "sourceVersion": updated_project.source_version,
        "parentVersion": updated_project.parent_version,
        "imageUrl": f"/api/projects/{updated_project.id}/image",
        "width": updated_project.image_width,
        "height": updated_project.image_height,
        "edit": _draft_payload(updated_draft),
    }


@router.delete("/edits/{edit_id}")
def discard_edit(edit_id: str, request: Request) -> dict[str, object]:
    store = request.app.state.store
    _editable_draft(store, edit_id)
    store.delete_edit_draft(edit_id)
    return {"discarded": edit_id}
