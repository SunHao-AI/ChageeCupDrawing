"""逐段笔序：按局部笔迹密度给每段笔画分配连续目标笔宽，并生成「先细后粗」的绘制顺序。

起笔用最细档（符合「自动变粗」直觉、也保证标定后第一笔不是残留的粗档），
随笔画进度逐档变粗；暗部（高密度）最粗、最后画，恰好压在最上层。目标笔宽
来自绘制前笔宽标定；未启用自动变粗时原样返回，保证既有任务的路径顺序逐位不变。

密度信号把笔迹本身栅格化成 1px 线后取大窗口均值（局部线密度）。不能用填充
掩膜做膨胀密度：掩膜是整块暗区，膨胀后区内处处饱和为 1，所有笔迹落进同一桶，
分桶完全失效。栅格化信号天然反映「线挤在一起=暗部」的手绘逻辑。

密度值归一化 → 目标滑杆行程 [0,1]，按容差分桶；桶内顺序用与规划同款的最近邻
贪心（geometry.nearest_order_paths）；跨桶不重排——桶间保持「细→粗」。

行程映射不用百分位排名：排名把全部笔画强制摊满 0~1 行程，一半笔画注定拿粗档，
真机实测最粗 23px（规划假设笔宽仅 2~3px），细线区被粗笔糊成黑块。改用密度值对
「最密参考值」（正密度 p99）的比值做 γ 压缩 + 上限截断：绝大多数笔画留细档，
只有真正挤死的暗部进粗档。另按「笔宽不粗于笔画本身」限宽：短于
``SHORT_STROKE_PX`` 的笔画若用宽笔画，一个墨点就糊掉周围白区（点画感主因），
其行程上限取实测宽度 ≤ 长度×1.15 的最大档。
"""

from __future__ import annotations

import math
from typing import Sequence

import cv2
import numpy as np

from app.core.pathing.geometry import nearest_order_paths

Path = list[tuple[float, float]]

# 局部线密度统计窗口（画布 px）：窗口越大越平滑，段间区分度越稳。
DENSITY_WIN = 21
# 密度值→行程的 γ 压缩与上限（真机仿真选参：γ=0.7/cap=0.75 时脸部笔画保持
# 细档、头发暗部仍能吃粗，粗笔（≥14px）占比从 rank 映射的 ~50% 降到 6%）。
DENSITY_GAMMA = 0.7
DENSITY_CAP = 0.75
# 短线限宽：长度低于该值的笔画按「实测宽度 ≤ 长度×1.15」封顶。
SHORT_STROKE_PX = 24.0
SHORT_WIDTH_RATIO = 1.15


def _path_length(path: Path) -> float:
    if len(path) < 2:
        return 0.0
    a = np.asarray(path, dtype=np.float64)
    return float(np.hypot(*np.diff(a, axis=0).T).sum())


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


def target_fractions(
    paths: list[Path], width_table: Sequence[tuple[float, float]] | None = None
) -> list[float]:
    """每段笔画 → 目标滑杆行程 [0,1]（1=最粗）。

    行程 = (密度 / 最密参考值) ** γ，上限 ``DENSITY_CAP``；``width_table`` 存在时
    再按「实测笔宽 ≤ 长度×1.15」给短线封顶。信号退化时全 0（最细档）。
    """
    paths = [p for p in paths if p]
    if not paths:
        return []
    ys = [round(y) for p in paths for _, y in p]
    xs = [round(x) for p in paths for x, _ in p]
    box = _line_density_map(paths, (max(ys) + 2, max(xs) + 2))
    densities = np.array([_path_density(p, box) for p in paths], dtype=np.float64)
    if float(densities.std()) < 1e-6:
        return [0.0] * len(paths)          # 信号退化：全用最细档
    positive = densities[densities > 0]
    # 参考值取正密度 p99：真机头发芯有超密离群点，max 会把其余笔画全部压低。
    ref = float(np.percentile(positive, 99)) if positive.size else 0.0
    if ref <= 0:
        return [0.0] * len(paths)
    ratio = np.clip(densities / ref, 0.0, 1.0) ** DENSITY_GAMMA
    fracs = np.minimum(ratio * DENSITY_CAP, 1.0)
    if width_table and len(width_table) >= 2:
        # 短线限宽：宽笔画的墨点半径会糊掉短线周围的白区，实测宽度不得超过
        # 线长太多。滑杆宽度非单调，故在「实测宽度 ≤ 预算」的档里取行程最大者。
        table = sorted(((float(f), float(w)) for f, w in width_table), key=lambda t: t[1])
        for i, path in enumerate(paths):
            length = _path_length(path)
            if length >= SHORT_STROKE_PX:
                continue
            budget = max(table[0][1], length * SHORT_WIDTH_RATIO)
            ceiling = max((f for f, w in table if w <= budget), default=0.0)
            fracs[i] = min(fracs[i], ceiling)
    return [float(f) for f in fracs]


def width_buckets(
    paths: list[Path],
    tol: float,
    width_table: Sequence[tuple[float, float]] | None = None,
) -> list[tuple[int, float, list[Path]]]:
    """→ [(桶号, 桶下缘行程, 桶内笔迹)]，按桶号升序（细→粗）。桶号=floor(f/tol)。

    tol 夹在 [0.02, 0.20]，NaN 回退默认 0.05。桶上限由 tol 推导：
    cap = ceil(1/tol) - 1，保证桶下缘 bucket*tol ≤ 1（tol=0.05 → 19 档、
    tol=0.02 → 49 档、tol=0.20 → 4 档）；整图换笔次数 ≈ cap+1，粒度越细越多。
    ``width_table`` 透传给 target_fractions 做短线限宽。
    """
    paths = [p for p in paths if p]
    tol = 0.05 if not math.isfinite(tol) else float(tol)
    tol = max(0.02, min(0.20, tol))
    # 减 1e-9 抵消 1/0.1=10.000000000000002 这类浮点上偏，避免多出一个空档。
    cap = math.ceil(1.0 / tol - 1e-9) - 1
    fracs = target_fractions(paths, width_table)
    groups: dict[int, list[Path]] = {}
    for frac, path in zip(fracs, paths):
        # int() 截断在 0.6/0.2=2.9999999999999996 这类下偏时会把恰落桶下缘的
        # 行程掉进下一档；加 1e-9 修正到 floor(f/tol) 的真值。
        groups.setdefault(min(cap, int(frac / tol + 1e-9)), []).append(path)
    out: list[tuple[int, float, list[Path]]] = []
    for bucket_id in sorted(groups):                     # 细→粗
        out.append((bucket_id, bucket_id * tol, nearest_order_paths(groups[bucket_id])))
    return out
