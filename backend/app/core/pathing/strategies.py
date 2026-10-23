"""策略注册表：四种路径策略以纯函数形式暴露，便于单独测试与替换。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.core.pathing.centerline import greedy_skeleton_paths, zhang_suen_thinning
from app.core.pathing.contour import contour_paths
from app.core.pathing.geometry import simplify_polyline
from app.core.pathing.hscan import horizontal_runs
from app.core.pathing.topology import concentric_fill_paths
from app.core.pathing.types import Paths

STRATEGY_CENTERLINE = "centerline"
STRATEGY_CONTOUR = "contour"
STRATEGY_HSCAN = "hscan"
STRATEGY_TOPOLOGY = "topology"
STRATEGY_TONE = "tone"

_STRATEGY_ALIASES = {
    "centerline": STRATEGY_CENTERLINE,
    "中心线（线稿）": STRATEGY_CENTERLINE,
    "中心线": STRATEGY_CENTERLINE,
    "contour": STRATEGY_CONTOUR,
    "轮廓（空心）": STRATEGY_CONTOUR,
    "轮廓": STRATEGY_CONTOUR,
    "hscan": STRATEGY_HSCAN,
    "水平填充（兼容）": STRATEGY_HSCAN,
    "水平填充": STRATEGY_HSCAN,
    "topology": STRATEGY_TOPOLOGY,
    "拓扑保真填充（推荐）": STRATEGY_TOPOLOGY,
    "拓扑保真填充": STRATEGY_TOPOLOGY,
    "保真填充（推荐）": STRATEGY_TOPOLOGY,
    "保真填充": STRATEGY_TOPOLOGY,
    "tone": STRATEGY_TONE,
    "照片素描": STRATEGY_TONE,
}


def normalize_strategy(strategy: str) -> str:
    return _STRATEGY_ALIASES.get(str(strategy).strip(), STRATEGY_TOPOLOGY)


@dataclass(frozen=True)
class StrategyContext:
    """策略在超采样坐标系下需要的尺度参数。"""

    brush_width_work: float
    simplify_px_work: float
    fill_density: float
    thin_finish: bool = True


def _centerline(mask: np.ndarray, ctx: StrategyContext) -> Paths:
    skel = zhang_suen_thinning(mask)
    raw = greedy_skeleton_paths(skel)
    return [simplify_polyline(p, max(0.12, ctx.simplify_px_work * 0.70), False) for p in raw if p]


def _contour(mask: np.ndarray, ctx: StrategyContext) -> Paths:
    return contour_paths(mask, ctx.simplify_px_work)


def _hscan(mask: np.ndarray, ctx: StrategyContext) -> Paths:
    pitch = max(1, int(round(ctx.brush_width_work * max(0.62, min(1.1, ctx.fill_density)))))
    return horizontal_runs(mask, pitch)


def _topology(mask: np.ndarray, ctx: StrategyContext) -> Paths:
    return concentric_fill_paths(mask, ctx.brush_width_work, ctx.fill_density, ctx.simplify_px_work, ctx.thin_finish)


StrategyFn = Callable[[np.ndarray, StrategyContext], Paths]

STRATEGIES: dict[str, StrategyFn] = {
    STRATEGY_CENTERLINE: _centerline,
    STRATEGY_CONTOUR: _contour,
    STRATEGY_HSCAN: _hscan,
    STRATEGY_TOPOLOGY: _topology,
}


def build_paths(strategy: str, mask: np.ndarray, ctx: StrategyContext) -> Paths:
    return STRATEGIES[normalize_strategy(strategy)](mask, ctx)
