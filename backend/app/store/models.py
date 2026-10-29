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
    # 产出本份校准那一刻的截图帧尺寸（0=旧库无此信息）：校准页标注层据此
    # 对齐坐标系，避免回退实时截图 naturalSize 时两帧不一致导致缩放失真。
    frame_w: int = 0
    frame_h: int = 0
    # 自动变粗（粗细滑杆标定）：截图坐标系下的轨道区域 {"x","y","w","h"} 与撤销按钮 {"x","y"}。
    slider: dict | None = None
    undo: dict | None = None
    # 画板下方工具面板折叠按钮中心点 {"x","y"}：换笔间隙自动收起面板。
    fold: dict | None = None
    # 面板展开/折叠两态工具位图（人工兜底可覆盖单项）。展开态由 ToolbarProber
    # 行为探测产出；tools_collapsed 由一键自动校准末尾的「收起面板→重扫」追加
    # 产出（收起态重扫失败时仍为 None），校准 API 也可透传覆盖。
    tools_open: dict | None = None
    tools_collapsed: dict | None = None
    # 自动校准快照 ID：产出本份校准那一刻的截图帧存为项目目录下
    # snapshot_{id}.jpg，校准页标注层据此锚定同一帧（帧不符则淡出标注）。
    snapshot_id: str | None = None
    # 绘制目标类型：pc=本机窗口；mirror=scrcpy 投屏窗口（注入与守护同 PC 窗口）；
    # adb=直连预留。serial 标识来源设备，供设备丢失时给出准确提示。
    device_kind: str = "pc"
    device_serial: str | None = None
    # 空白画布基线掩码（canvas_blankness L2）：{"w","h","mask"}，mask 为 1/8
    # 下采样墨迹布尔掩码的 base64。判空白时与当前帧掩码比对，水印/纹理等常驻
    # 背景进基线后不再干扰。
    blank_baseline: dict | None = None


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
