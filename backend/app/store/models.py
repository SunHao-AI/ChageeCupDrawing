"""存储层实体（与数据库行一一对应）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    image_path: str
    image_width: int
    image_height: int
    created_at: str
    source_version: int = 1
    parent_version: int | None = None


@dataclass(frozen=True)
class CanvasProfile:
    id: str
    project_id: str
    hwnd: int
    window_title: str
    capture_method: str
    rect_x: int
    rect_y: int
    rect_w: int
    rect_h: int
    dpi_scale: float
    source: str
    confidence: float
    captured_at: str


@dataclass(frozen=True)
class PlanRecord:
    id: str
    project_id: str
    params: dict
    strategy: str
    metrics: dict
    paths_path: str
    target_w: int
    target_h: int
    created_at: str


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    params: dict
    builtin: bool


@dataclass(frozen=True)
class EditDraft:
    id: str
    project_id: str
    base_version: int
    channel: str
    instruction: str
    roi: dict | None
    provider: str
    draft_path: str
    diff_path: str | None
    created_at: str
    applied_at: str | None


@dataclass(frozen=True)
class TaskRecord:
    id: str
    plan_id: str
    exec_params: dict
    status: str
    cursor_path: int
    stop_reason: str | None
    error_code: str | None
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class TaskEvent:
    seq: int
    task_id: str
    type: str
    payload: dict
    ts: str
