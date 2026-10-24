"""笔宽测量：从画布截图中测出最近一段笔迹的实际墨迹宽度。

自动变粗标定的基础能力：在滑杆某一档位画一条水平测试线，截图后找到墨迹
连通域，取其垂直方向（列内）的中位厚度作为该档笔宽。只统计测试区窗口内
的像素，避免画布上旧笔迹干扰；返回 None 表示窗口内没有墨迹（该档未落笔
成功或线太淡）。
"""

from __future__ import annotations

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
