"""照片素描（tone contour）：多级色调等值线，用线密度表达明暗。

照片转二值掩膜会丢掉全部灰度信息，固定笔宽的填充只会把中间调画成一片
噪声碎线。本策略绕过掩膜直接消费工作分辨率灰度图：双边滤波压噪保边后，
把灰度归一化量化成 L 级，取相邻量化级之间的分界线作为等值线——平坦区
无级差即无线，渐变区每跨一级出一条线，暗区跨级多线条自然加密。这正是
桌面绘图仪社区 topo-line / Squiggle cam3D 的成像原理：笔宽不变，用线条
疏密表达明暗，且每条线都是连续的长曲线。
"""

from __future__ import annotations

import cv2
import numpy as np

from app.core.pathing.centerline import greedy_skeleton_paths
from app.core.pathing.geometry import simplify_polyline
from app.core.pathing.types import Path, Paths


def tone_field(
    gray: np.ndarray, levels: int, background: np.ndarray | None = None
) -> np.ndarray:
    """归一化量化到 [0, levels-1] 的色调级图（0=最暗），float32 供面积分位复用。

    三级预平滑各挡一类伪影：中值滤波拆掉 JPEG 8px 压缩块（否则块边界长出
    方阵轮廓），高斯抹掉单像素抖动，双边滤波在保住真实棱线的前提下收尾。
    少了任何一级都会在量化边界长出阶梯状碎圈。

    ``background`` 给定时，明暗参考区间只在内容像素上统计：contain 铺到很宽的
    画布时留白能占到八九成，把背景算进百分位会把整张内容压成同一级，等值线
    几乎归零。
    """
    smooth = cv2.medianBlur(gray, 5)
    smooth = cv2.GaussianBlur(smooth, (0, 0), 1.6)
    smooth = cv2.bilateralFilter(smooth, 9, 45, 9)
    samples = smooth
    if background is not None:
        content = background == 0
        if np.count_nonzero(content) > smooth.size * 0.02:
            samples = smooth[content]
    lo = float(np.percentile(samples, 4))
    hi = float(np.percentile(samples, 96))
    if hi - lo < 12.0:
        hi = lo + 12.0
    norm = np.clip((smooth.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)
    return np.minimum((norm * levels).astype(np.int32), levels - 1).astype(np.float32)


def tone_boundary(tone: np.ndarray) -> np.ndarray:
    """相邻像素量化级不同的位置即等值线，1 px 宽，天然无重复线。"""
    quantized = np.floor(tone).astype(np.int32)
    right = quantized != np.roll(quantized, -1, axis=1)
    down = quantized != np.roll(quantized, -1, axis=0)
    boundary = (right | down).astype(np.uint8) * 255
    boundary[:, -1] = 0
    boundary[-1, :] = 0
    return boundary


def tone_area(
    tone: np.ndarray, levels: int, background: np.ndarray | None = None
) -> np.ndarray:
    """把色调级重新按「面积分位」量化回 levels 档。

    直方图规定化：等值线落在等面积带上而不是等灰度带上，线条间距随局部明暗
    自动收放——平坦噪声区跨不过级差（无线），暗部跨级密集（线密），中间调
    保持可读的疏密层次。

    ``background`` 给定时直方图只统计内容像素：contain 留白占比过高时会垄断
    分位，把内容全部推到极端档位。
    """
    flat = tone.ravel().astype(np.int32)
    counts = np.bincount(flat, minlength=int(levels)).astype(np.float64)
    if background is not None:
        content = (background == 0).ravel()
        if int(content.sum()) > flat.size * 0.02:
            counts = np.bincount(
                flat[content], minlength=int(levels)
            ).astype(np.float64)
    cumulative = np.cumsum(counts)
    rank = (cumulative - counts * 0.5) / max(1.0, float(counts.sum()))
    rescaled = np.clip(rank[flat] * levels, 0.0, levels - 1.0)
    return rescaled.reshape(tone.shape)


def chaikin_smooth(path: Path, iterations: int = 2) -> Path:
    """角切割平滑：等值线沿像素网格走会带阶梯，割角能把锯齿揉成顺曲线。

    笔迹本身是矢量，落笔时按点采样，所以平滑只需在规划期做一次，成本是纯
    几何运算。端点保持不动，避免笔迹首尾漂移。
    """
    if len(path) < 3 or iterations < 1:
        return path
    out = path
    for _ in range(int(iterations)):
        nxt: Path = [out[0]]
        for (x1, y1), (x2, y2) in zip(out, out[1:]):
            nxt.append((0.75 * x1 + 0.25 * x2, 0.75 * y1 + 0.25 * y2))
            nxt.append((0.25 * x1 + 0.75 * x2, 0.25 * y1 + 0.75 * y2))
        nxt.append(out[-1])
        out = nxt
    return out


def compute_background(gray: np.ndarray) -> np.ndarray:
    """标记 contain 适配填充的纯色留白（与 ≥2 个画布角同值连通的区域），供等值线排除。

    contain 会在图像四周补常量白，量化后 padding 与内容边缘相差一整级，会生成
    一条沿内容周边贯穿的边框线——正是实拍照片里那条横贯顶部的粗黑线。要求至少
    两角同值连通，避免把只贴着一个角的浅色高光误判成背景；下限取 2 而非 3，是
    因为竖幅照片撑满画布高度时会把留白左右隔断，每块留白各触 2 角。
    """
    h, w = gray.shape
    corners = [
        int(gray[0, 0]), int(gray[0, w - 1]), int(gray[h - 1, 0]), int(gray[h - 1, w - 1]),
    ]
    corner = int(np.median(corners))
    near = (np.abs(gray.astype(np.int16) - corner) <= 6).astype(np.uint8)
    labels = cv2.connectedComponents(near, 8)[1]
    corner_points = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    touched: dict[int, int] = {}
    for y, x in corner_points:
        label = int(labels[y, x])
        if label:
            touched[label] = touched.get(label, 0) + 1
    keep_labels = [label for label, hits in touched.items() if hits >= 2]
    if not keep_labels:
        return np.zeros_like(gray)
    return np.isin(labels, keep_labels).astype(np.uint8) * 255


def tone_contour_paths(
    tone: np.ndarray,
    simplify_px: float,
    min_pixels: int = 8,
    background: np.ndarray | None = None,
) -> Paths:
    """提取多级等值线并串成连续笔迹（输入为 tone_field / tone_area 的输出）。"""
    boundary = tone_boundary(tone)
    if background is not None:
        # 吃掉背景与内容接缝上的边框线：预平滑把明暗过渡带展宽约 ±5 px，
        # 排除半径必须盖过过渡带，否则接缝下方仍会漏出一条贯穿线。
        seam = cv2.dilate(background, np.ones((5, 5), np.uint8), iterations=4)
        boundary[seam > 0] = 0
    # 量化噪声产生的孤立小圈直接丢弃；真实的轮廓簇连成大片。
    count, labels, stats, _ = cv2.connectedComponentsWithStats(boundary, 8)
    keep = np.zeros_like(boundary)
    floor = max(4, int(min_pixels))
    for i in range(1, count):
        if int(stats[i, cv2.CC_STAT_AREA]) >= floor:
            keep[labels == i] = 255
    if cv2.countNonZero(keep) == 0:
        return []

    paths: Paths = []
    for raw in greedy_skeleton_paths(keep):
        if len(raw) < 2:
            continue
        smoothed = simplify_polyline(raw, max(0.12, float(simplify_px) * 1.2), False)
        if len(smoothed) >= 2:
            paths.append(chaikin_smooth(smoothed, 2))
    return paths
