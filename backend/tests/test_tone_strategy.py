"""照片素描（tone contour）策略：等值线语义、背景排除与端到端冒烟。"""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.core.pathing.hatch import (
    chaikin_smooth,
    compute_background,
    tone_area,
    tone_contour_paths,
    tone_field,
)
from app.core.planning.planner import build_drawing_plan


def _gradient(width: int = 200, height: int = 120) -> np.ndarray:
    """水平线性渐变：左暗右亮。"""
    ramp = np.linspace(20, 235, width, dtype=np.uint8)
    return np.tile(ramp, (height, 1))


def test_gradient_yields_multiple_vertical_contours() -> None:
    levels = 10
    tone = tone_area(tone_field(_gradient(), levels), levels)
    paths = tone_contour_paths(tone, 0.2)
    assert len(paths) >= levels - 2  # 每跨一个量化级一条线
    xs = sorted(p[0][0] for p in paths if len(p) >= 2)
    # 线应大致等距地铺满渐变方向。
    assert xs[0] > 0 and xs[-1] < 199
    gaps = np.diff(xs)
    assert gaps.mean() > 3  # 相邻线有间距，不是同一条线被切碎


def test_flat_image_yields_no_paths() -> None:
    flat = np.full((80, 80), 200, dtype=np.uint8)
    tone = tone_area(tone_field(flat, 10), 10)
    assert tone_contour_paths(tone, 0.2) == []


def test_background_padding_removes_frame_line() -> None:
    # 渐变内容居中、四周一圈常量白（contain 的真实布局）：padding 与内容的
    # 接缝不得生成沿内容周边贯穿的边框线。
    gray = np.full((160, 200), 255, dtype=np.uint8)
    gray[40:120, 20:180] = _gradient(160, 80)
    tone = tone_area(tone_field(gray, 10), 10)
    without = tone_contour_paths(tone, 0.2)
    with_bg = tone_contour_paths(tone, 0.2, background=compute_background(gray))

    def long_span(paths, axis):
        idx = 0 if axis == "x" else 1
        return [
            p for p in paths
            if max(pt[idx] for pt in p) - min(pt[idx] for pt in p) > 150
        ]

    assert long_span(without, "x")  # 不加背景排除时确实出现贯穿横线（问题的存在性）
    assert not long_span(with_bg, "x")  # 加背景排除后被裁掉
    assert len(with_bg) > 0  # 内容区自身的等值线保留


def test_wide_canvas_padding_does_not_collapse_content() -> None:
    # 超宽画布（杯贴环形展开）上内容只占一小块：留白占比 >85% 时归一化与
    # 面积分位必须只看内容像素，否则整张内容被压成同一级，等值线崩塌。
    content = _gradient(160, 120)
    narrow = tone_contour_paths(
        tone_area(tone_field(content, 10), 10), 0.2
    )

    canvas = np.full((120, 3000), 255, dtype=np.uint8)
    canvas[:, 20:180] = content
    bg = compute_background(canvas)
    tone = tone_area(tone_field(canvas, 10, background=bg), 10, background=bg)
    padded = tone_contour_paths(tone, 0.2, background=bg)

    without = tone_contour_paths(
        tone_area(tone_field(canvas, 10), 10), 10, 0.2
    )
    assert len(without) < len(narrow) * 0.3  # 不过滤背景时确实崩塌（存在性）
    assert len(padded) >= len(narrow) * 0.7  # 过滤后恢复到窄画布量级


def test_chaikin_keeps_endpoints_and_smooths() -> None:
    stair: list[tuple[float, float]] = [(float(x), float(x % 2)) for x in range(10)]
    smoothed = chaikin_smooth(stair, 2)
    assert smoothed[0] == stair[0]
    assert smoothed[-1] == stair[-1]
    assert len(smoothed) > len(stair)
    # 割角后相邻点的方向变化（转角）应比锯齿原线更平缓。
    def max_turn(pts):
        angles = []
        for (x0, y0), (x1, y1), (x2, y2) in zip(pts, pts[1:], pts[2:]):
            a1 = np.arctan2(y1 - y0, x1 - x0)
            a2 = np.arctan2(y2 - y1, x2 - x1)
            angles.append(abs(a2 - a1))
        return max(angles)
    assert max_turn(smoothed) < max_turn(stair) + 1e-9


def test_build_plan_smoke_tone_strategy() -> None:
    image = Image.fromarray(_gradient(240, 160), mode="L")
    plan = build_drawing_plan(
        image, 130, 90,
        image_mode="adaptive", threshold=205, contrast=1.0, fit_mode="contain",
        line_expand=0, strategy="tone", brush_width=3, simplify_px=0.16, safe_margin=4,
        fill_density=0.82, legacy_row_step=3, supersample=2, detail_keep=0.14,
        auto_trim=False, topology_rescue=False, hatch_levels=10,
    )
    assert plan.paths
    assert plan.strategy == "tone"
    assert plan.simulated.size == (130, 90)
    ink = float((np.asarray(plan.simulated) < 128).mean())
    assert 0.01 < ink < 0.5  # 有线条但没糊满
