"""笔宽测量与画布墨迹差分。

自动变粗标定的基础能力：在滑杆某一档位画一条水平测试线，截图后找到墨迹
连通域，取其垂直方向（列内）的中位厚度作为该档笔宽。只统计测试区窗口内
的像素，避免画布上旧笔迹干扰；返回 None 表示窗口内没有墨迹（该档未落笔
成功或线太淡）。

墨迹差分（undo-rollback-design §4）：比较一条笔迹注入前后的两帧截图，把
新增墨迹中落在该笔迹膨胀邻域之外的部分计为「预期外墨迹」（手指误触），把
大面积亮度翻转计为「画布整体变化」（弹窗覆盖/清空）。
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

INK_THRESHOLD = 160


def measure_ink_width(image: np.ndarray, region: tuple[int, int, int, int]) -> float | None:
    """测量 image（灰度或 BGR ndarray）中 region=(x,y,w,h) 窗口内墨迹的垂直厚度中位数。

    对每列统计连续墨迹段的长度，取所有段的中位数：水平测试线即便有断点或
    端点圆帽，中位厚度仍稳定反映笔宽。列内墨迹段按「本列墨迹像素的行程」
    计算，避免斜线测不准——测试线由调用方保证水平。
    """
    gray = image[..., 0] if image.ndim == 3 else image
    x, y, w, h = (int(v) for v in region)
    x = max(0, x)
    y = max(0, y)
    x2 = min(gray.shape[1], x + w)
    y2 = min(gray.shape[0], y + h)
    if x2 <= x or y2 <= y:
        return None
    crop = gray[y:y2, x:x2]
    ink = crop < INK_THRESHOLD
    if int(ink.sum()) < 8:
        return None

    runs: list[int] = []
    for col in range(ink.shape[1]):
        column = ink[:, col]
        if not column.any():
            continue
        # 行号差分找连续段：段边界在 diff != 1 处。
        rows = np.flatnonzero(column)
        breaks = np.flatnonzero(np.diff(rows) > 1)
        starts = np.concatenate(([0], breaks + 1))
        ends = np.concatenate((breaks + 1, [len(rows)]))
        runs.extend(int(rows[e - 1] - rows[s] + 1) for s, e in zip(starts, ends) if e > s)
    if not runs:
        return None
    return float(np.median(runs))


def ink_occupied_fraction(image: np.ndarray, region: tuple[int, int, int, int]) -> float:
    """region 内墨迹像素占比，用于撤销后验证画布已清空。"""
    gray = image[..., 0] if image.ndim == 3 else image
    x, y, w, h = (int(v) for v in region)
    x2 = min(gray.shape[1], max(0, x) + max(1, w))
    y2 = min(gray.shape[0], max(0, y) + max(1, h))
    x, y = max(0, x), max(0, y)
    if x2 <= x or y2 <= y:
        return 0.0
    return float((gray[y:y2, x:x2] < INK_THRESHOLD).mean())


def ink_pixels(image: np.ndarray, region: tuple[int, int, int, int]) -> int:
    """region 内墨迹像素绝对数；回退生效判定用它报「画布墨迹 −N px」。"""
    gray = image[..., 0] if image.ndim == 3 else image
    x, y, w, h = (int(v) for v in region)
    x2 = min(gray.shape[1], max(0, x) + max(1, w))
    y2 = min(gray.shape[0], max(0, y) + max(1, h))
    x, y = max(0, x), max(0, y)
    if x2 <= x or y2 <= y:
        return 0
    return int((gray[y:y2, x:x2] < INK_THRESHOLD).sum())


def detect_stray_ink(
    before: np.ndarray,
    after: np.ndarray,
    stroke_points: Sequence[tuple[float, float]],
    region: tuple[int, int, int, int],
    *,
    radius_px: int = 16,
    min_px: int = 40,
) -> tuple[int, bool]:
    """比较一条笔迹注入前后的画布帧，返回 (预期外墨迹像素数, 画布是否整体变化)。

    新增墨迹 = after 的墨迹 ∧ ¬before 的墨迹（region 内）；以注入笔迹为骨架
    做半径 ``radius_px`` 的膨胀作为允许区域，区域外的新增墨迹计为预期外
    （手指误触）。少于 ``min_px`` 的杂散更可能来自渲染抖动，不计。

    画布整体变化（弹窗覆盖、清空）表现为大面积亮度翻转（无论变亮变暗），
    翻转占比超过 50% 时第二个返回值为 True——它不是误触笔画，停止原因不同。
    """
    gray_b = before[..., 0] if before.ndim == 3 else before
    gray_a = after[..., 0] if after.ndim == 3 else after
    x, y, w, h = (int(v) for v in region)
    x2 = min(min(gray_b.shape[1], gray_a.shape[1]), max(0, x) + max(1, w))
    y2 = min(min(gray_b.shape[0], gray_a.shape[0]), max(0, y) + max(1, h))
    x, y = max(0, x), max(0, y)
    if x2 <= x or y2 <= y:
        return 0, False

    crop_b = gray_b[y:y2, x:x2]
    crop_a = gray_a[y:y2, x:x2]
    flipped = (crop_a < INK_THRESHOLD) != (crop_b < INK_THRESHOLD)
    total = crop_a.size
    if total > 0 and flipped.sum() / total > 0.5:
        return 0, True

    added = (crop_a < INK_THRESHOLD) & ~(crop_b < INK_THRESHOLD)
    if not added.any():
        return 0, False

    allow = np.zeros_like(added, dtype=np.uint8)
    if stroke_points:
        pts = np.array(
            [[int(px) - x, int(py) - y] for px, py in stroke_points],
            dtype=np.int32,
        ).reshape(-1, 1, 2)
        cv2.polylines(allow, [pts], isClosed=False, color=1, thickness=2 * radius_px + 1)
    stray = int(added[allow == 0].sum())
    return (stray if stray > min_px else 0), False
