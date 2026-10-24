"""分档笔序：按局部笔迹密度给笔迹分粗细档，并生成「先粗后细」的绘制顺序。

先画粗笔快速铺暗部底，再逐档细化，视觉更接近手绘。档数来自绘制前笔宽标定；
未启用自动变粗时原样返回，保证既有任务的路径顺序逐位不变。

密度信号把笔迹本身栅格化成 1px 线后取大窗口均值（局部线密度）。不能用填充
掩膜做膨胀密度：掩膜是整块暗区，膨胀后区内处处饱和为 1，所有笔迹落进同一档，
分档完全失效。栅格化信号天然反映「线挤在一起=暗部」的手绘逻辑。

档内顺序用与规划同款的最近邻贪心（geometry.nearest_order_paths）；跨档不
重排——档间保持「粗→细」，档内局部最优。
"""

from __future__ import annotations

import cv2
import numpy as np

from app.core.pathing.geometry import nearest_order_paths

Path = list[tuple[float, float]]

# 局部线密度统计窗口（画布 px）：窗口越大越平滑，档间区分度越稳。
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


def tier_groups(
    paths: list[Path],
    tiers: int,
) -> list[tuple[int, list[Path]]]:
    """按分位点把笔迹分成 K 档，返回 [(档索引, 档内笔迹)]，档索引大=更粗、先绘制。

    空档跳过；档内按最近邻贪心重排；密度信号无区分度时保持原顺序单档返回。
    """
    tier_count = max(2, int(tiers))
    if tier_count <= 1 or not paths:
        return [(0, list(paths))]

    ys = [round(y) for p in paths for _, y in p]
    xs = [round(x) for p in paths for x, _ in p]
    h = max(ys) + 2
    w = max(xs) + 2
    box = _line_density_map(paths, (h, w))
    densities = np.array([_path_density(p, box) for p in paths], dtype=np.float64)
    if float(densities.std()) < 1e-6:
        # 信号退化（如全部笔迹完全重合）：不重排，单档原样返回。
        return [(0, list(paths))]
    # 分位点切档保证每档笔迹数量均衡（纯阈值易被长轮廓垄断粗档）。
    edges = np.quantile(densities, [i / tier_count for i in range(1, tier_count)])
    tier_of = np.searchsorted(edges, densities, side="left")

    groups: list[tuple[int, list[Path]]] = []
    for draw_slot in range(tier_count):
        tier = tier_count - 1 - draw_slot  # 粗档先画
        members = [paths[i] for i in np.flatnonzero(tier_of == tier)]
        if members:
            groups.append((int(tier), nearest_order_paths(members)))
    return groups
