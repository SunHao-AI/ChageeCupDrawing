"""轮廓策略：findContours + approxPolyDP。"""

from __future__ import annotations

import cv2
import numpy as np

from app.core.pathing.types import Paths


def contour_paths(mask: np.ndarray, simplify_px: float, min_perimeter: float = 2.0) -> Paths:
    contours, _hier = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    paths: Paths = []
    for c in contours:
        if len(c) == 1:
            x, y = c[0, 0]
            paths.append([(float(x), float(y))])
            continue
        per = float(cv2.arcLength(c, True))
        if per < min_perimeter:
            continue
        approx = cv2.approxPolyDP(c, max(0.15, float(simplify_px)), True).reshape(-1, 2)
        if len(approx) == 1:
            x, y = approx[0]
            paths.append([(float(x), float(y))])
        elif len(approx) >= 2:
            p = [(float(x), float(y)) for x, y in approx]
            p.append(p[0])
            paths.append(p)
    return paths
