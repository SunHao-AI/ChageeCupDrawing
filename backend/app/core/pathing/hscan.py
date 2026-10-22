"""水平填充策略：逐行扫描的水平线段。"""

from __future__ import annotations

import numpy as np

from app.core.pathing.types import Paths


def horizontal_runs(mask: np.ndarray, y_step: int = 1) -> Paths:
    h, w = mask.shape
    paths: Paths = []
    for y in range(0, h, max(1, y_step)):
        row = mask[y] > 0
        xs = np.flatnonzero(row)
        if not len(xs):
            continue
        start = prev = int(xs[0])
        for xv in xs[1:]:
            x = int(xv)
            if x == prev + 1:
                prev = x
            else:
                paths.append([(float(start), float(y)), (float(prev), float(y))])
                start = prev = x
        paths.append([(float(start), float(y)), (float(prev), float(y))])
    return paths
