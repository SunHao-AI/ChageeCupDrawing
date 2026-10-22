"""规划编排：选策略 → 生成 → 缩放 → 拓扑补偿 → 排序 → 指标。

迁移自 `cup_drawbot.py` 的 `build_drawing_plan`。执行顺序与 V6 完全一致，因为
顺序本身参与结果：拓扑补偿必须在缩放到画布坐标之后、排序之前发生。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image

from app.core.imaging.binarize import process_image_hq
from app.core.pathing.geometry import binary_to_mask, nearest_order_paths, path_length, scale_paths
from app.core.pathing.render import render_paths
from app.core.pathing.strategies import (
    STRATEGY_TOPOLOGY,
    StrategyContext,
    build_paths,
    normalize_strategy,
)
from app.core.pathing.topology import topology_rescue_paths
from app.core.pathing.types import Paths
from app.core.planning.metrics import quality_metrics


@dataclass
class DrawingPlan:
    paths: Paths
    binary: Image.Image
    simulated: Image.Image
    strategy: str
    total_length: float
    margin: int
    precision: float = 0.0
    recall: float = 0.0
    iou: float = 0.0
    work_scale: int = 1
    correction_paths: int = 0
    target_recall: float = 0.985


def build_drawing_plan(
    original: Image.Image,
    canvas_w: int,
    canvas_h: int,
    *,
    image_mode: str,
    threshold: int,
    contrast: float,
    fit_mode: str,
    line_expand: int,
    strategy: str,
    brush_width: int,
    simplify_px: float,
    safe_margin: int,
    fill_density: float,
    legacy_row_step: int,
    supersample: int = 3,
    detail_keep: float = 0.14,
    thin_finish: bool = True,
    auto_trim: bool = True,
    topology_rescue: bool = True,
    target_recall: float = 0.985,
    threshold_mode: bool = False,
) -> DrawingPlan:
    # User's calibration sample is ~3 px wide on a ~560 px cup canvas, so v5 uses
    # 3 px as the default rather than v4's 7 px assumption.
    bw = max(1, int(brush_width))
    margin = max(int(safe_margin), int(math.ceil(bw / 2)) + 2)
    inner_w = max(24, int(canvas_w) - margin * 2)
    inner_h = max(36, int(canvas_h) - margin * 2)

    binary_inner, work_mask, ss = process_image_hq(
        original, inner_w, inner_h, image_mode, threshold, contrast, fit_mode,
        line_expand, supersample, detail_keep, auto_trim, threshold_mode,
    )
    ctx = StrategyContext(
        brush_width_work=float(bw * ss),
        simplify_px_work=max(0.10, float(simplify_px) * ss),
        fill_density=fill_density,
        thin_finish=thin_finish,
    )
    strategy_key = normalize_strategy(strategy)

    local_work = [p for p in build_paths(strategy_key, work_mask, ctx) if p]
    local_paths = scale_paths(local_work, 1.0 / ss, margin, margin)

    # Return both the conversion target and the simulated real-brush result at the
    # exact same canvas size so the user can compare detail loss directly.
    binary_canvas = Image.new("L", (int(canvas_w), int(canvas_h)), 255)
    binary_canvas.paste(binary_inner, (margin, margin))

    correction_count = 0
    if topology_rescue and strategy_key == STRATEGY_TOPOLOGY:
        target_mask = binary_to_mask(binary_canvas)
        extra, _p, _r, _i = topology_rescue_paths(
            target_mask, local_paths, bw, simplify_px, target_recall=target_recall
        )
        if extra:
            local_paths.extend(extra)
            correction_count = len(extra)

    local_paths = nearest_order_paths(local_paths)
    simulated = render_paths((int(canvas_w), int(canvas_h)), local_paths, bw)
    precision, recall, iou = quality_metrics(binary_canvas, simulated)
    total = sum(path_length(p) for p in local_paths)
    return DrawingPlan(
        local_paths, binary_canvas, simulated, strategy, total, margin,
        precision=precision, recall=recall, iou=iou, work_scale=ss,
        correction_paths=correction_count, target_recall=float(target_recall),
    )
