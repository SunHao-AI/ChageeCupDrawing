"""折线基础运算：掩膜互转、简化、排序、缩放。"""

from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import Image

from app.core.pathing.types import Path, Paths


def binary_to_mask(binary: Image.Image) -> np.ndarray:
    arr = np.asarray(binary, dtype=np.uint8)
    return (arr < 128).astype(np.uint8) * 255


def simplify_polyline(points: Path, epsilon: float, closed: bool = False) -> Path:
    if len(points) <= 2 or epsilon <= 0:
        return points
    arr = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    approx = cv2.approxPolyDP(arr, float(epsilon), bool(closed)).reshape(-1, 2)
    out = [(float(x), float(y)) for x, y in approx]
    if closed and len(out) >= 2 and out[0] != out[-1]:
        out.append(out[0])
    return out


def offset_paths(paths: Paths, dx: float, dy: float) -> Paths:
    return [[(x + dx, y + dy) for x, y in p] for p in paths]


def scale_paths(paths: Paths, scale: float, dx: float, dy: float) -> Paths:
    return [[(x * scale + dx, y * scale + dy) for x, y in p] for p in paths]


def path_length(path: Path) -> float:
    return sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(path, path[1:]))


def order_paths(paths: Paths) -> Paths:
    """Serpentine ordering reduces long cursor jumps between independent strokes."""
    if not paths:
        return []
    bucket_h = 36
    buckets: dict[int, list[Path]] = {}
    for p in paths:
        cy = sum(y for _x, y in p) / len(p)
        buckets.setdefault(int(cy // bucket_h), []).append(p)
    out: Paths = []
    for bi in sorted(buckets):
        row = buckets[bi]
        row.sort(key=lambda p: sum(x for x, _y in p) / len(p), reverse=bool(bi % 2))
        out.extend(row)
    return out


def nearest_order_paths(paths: Paths) -> Paths:
    """Greedy endpoint ordering with optional reversal.

    For the hundreds of paths typical of v5 this substantially cuts cursor travel.
    For very large jobs we fall back to the cheaper serpentine order.
    """
    paths = [p for p in paths if p]
    if len(paths) > 1800:
        return order_paths(paths)
    if not paths:
        return []
    remaining = paths[:]
    # Start near top-left so progress on the real canvas is visually predictable.
    idx = min(range(len(remaining)), key=lambda i: (remaining[i][0][1], remaining[i][0][0]))
    current = remaining.pop(idx)
    out = [current]
    cx, cy = current[-1]
    while remaining:
        best_i = 0
        best_rev = False
        best_d = float("inf")
        for i, p in enumerate(remaining):
            d0 = (p[0][0] - cx) ** 2 + (p[0][1] - cy) ** 2
            d1 = (p[-1][0] - cx) ** 2 + (p[-1][1] - cy) ** 2
            if d0 < best_d:
                best_d, best_i, best_rev = d0, i, False
            if d1 < best_d:
                best_d, best_i, best_rev = d1, i, True
        p = remaining.pop(best_i)
        if best_rev:
            p = list(reversed(p))
        out.append(p)
        cx, cy = p[-1]
    return out
