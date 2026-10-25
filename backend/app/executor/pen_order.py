"""逐段笔序：按局部笔迹密度给每段笔画分配连续目标笔宽，并生成「先粗后细」的绘制顺序。

先画粗笔快速铺暗部底，再逐段变细，视觉更接近手绘。目标笔宽来自绘制前笔宽标定；
未启用自动变粗时原样返回，保证既有任务的路径顺序逐位不变。

密度信号把笔迹本身栅格化成 1px 线后取大窗口均值（局部线密度）。不能用填充
掩膜做膨胀密度：掩膜是整块暗区，膨胀后区内处处饱和为 1，所有笔迹落进同一桶，
分桶完全失效。栅格化信号天然反映「线挤在一起=暗部」的手绘逻辑。

密度百分位排名 → 目标滑杆行程 [0,1]，按容差分桶；桶内顺序用与规划同款的最近邻
贪心（geometry.nearest_order_paths）；跨桶不重排——桶间保持「宽→窄」。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.core.pathing.geometry import nearest_order_paths

Path = list[tuple[float, float]]

# 局部线密度统计窗口（画布 px）：窗口越大越平滑，段间区分度越稳。
DENSITY_WIN = 21


def _path_density(path: Path, box: np.ndarray) -> float:
    h, w = box.shape
    values: list[float] = []
    step = max(1, len(path) // 12)
    for x, y in path[::step]:
        cx, cy = int(round(x)), int(round(y))
        if 0 <= cx < w and 0 <= cy < h:
            values.append(float(box[cy, cx]))
    return sum(values) / len(values) if values else 0.0


def _line_density_map(paths: list[Path], shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    canvas = np.zeros((h, w), np.uint8)
    for path in paths:
        if len(path) >= 2:
            cv2.polylines(canvas, [np.asarray(path, np.int32)], False, 255, 1)
    return cv2.boxFilter(
        (canvas > 0).astype(np.float32), -1, (DENSITY_WIN, DENSITY_WIN),
        borderType=cv2.BORDER_REFLECT,
    )


def target_fractions(paths: list[Path]) -> list[float]:
    """每段笔画 → 目标滑杆行程 [0,1]（1=最粗）。密度百分位排名；信号退化时全 0。"""
    paths = [p for p in paths if p]
    if not paths:
        return []
    ys = [round(y) for p in paths for _, y in p]
    xs = [round(x) for p in paths for x, _ in p]
    box = _line_density_map(paths, (max(ys) + 2, max(xs) + 2))
    densities = np.array([_path_density(p, box) for p in paths], dtype=np.float64)
    if float(densities.std()) < 1e-6:
        return [0.0] * len(paths)          # 信号退化：全用最细档
    # 稳定排序让并列密度按原始顺序取相邻名次，结果逐位确定（续画重算需一致）；
    # 不做平均名次——平均会让并列段共享同一行程，失去「每段一个连续目标」。
    ranks = densities.argsort(kind="stable").argsort().astype(np.float64)
    return [float(r) / max(1, len(paths) - 1) for r in ranks]


def width_buckets(paths: list[Path], tol: float) -> list[tuple[int, float, list[Path]]]:
    """→ [(桶号, 桶下缘行程, 桶内笔迹)]，按桶号降序（宽→窄）。桶号=floor(f/tol)。

    tol 夹在 [0.02, 0.20]，NaN 回退默认 0.05。桶上限由 tol 推导：
    cap = ceil(1/tol) - 1，保证桶下缘 bucket*tol ≤ 1（tol=0.05 → 19 档、
    tol=0.02 → 49 档、tol=0.20 → 4 档）；整图换笔次数 ≈ cap+1，粒度越细越多。
    """
    paths = [p for p in paths if p]
    tol = 0.05 if not math.isfinite(tol) else float(tol)
    tol = max(0.02, min(0.20, tol))
    # 减 1e-9 抵消 1/0.1=10.000000000000002 这类浮点上偏，避免多出一个空档。
    cap = math.ceil(1.0 / tol - 1e-9) - 1
    fracs = target_fractions(paths)
    groups: dict[int, list[Path]] = {}
    for frac, path in zip(fracs, paths):
        # int() 截断在 0.6/0.2=2.9999999999999996 这类下偏时会把恰落桶下缘的
        # 行程掉进下一档；加 1e-9 修正到 floor(f/tol) 的真值。
        groups.setdefault(min(cap, int(frac / tol + 1e-9)), []).append(path)
    out: list[tuple[int, float, list[Path]]] = []
    for bucket_id in sorted(groups, reverse=True):      # 宽→窄
        out.append((bucket_id, bucket_id * tol, nearest_order_paths(groups[bucket_id])))
    return out
