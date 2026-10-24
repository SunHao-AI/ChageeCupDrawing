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

    The greedy scan is vectorized with numpy but reproduces the scalar loop
    bit-exactly: candidates are scanned flattened as [d0_i, d1_i, ...] per path
    with a strict "<" keep-first rule, so `argmin` (first minimum) on the
    interleaved array matches the original tie semantics. Each arithmetic ufunc
    rounds to IEEE double independently, mirroring Python float arithmetic.
    """
    paths = [p for p in paths if p]
    if len(paths) > 1800:
        return order_paths(paths)
    n = len(paths)
    if not paths:
        return []
    starts = np.asarray([p[0] for p in paths], dtype=np.float64)
    ends = np.asarray([p[-1] for p in paths], dtype=np.float64)
    # Start near top-left so progress on the real canvas is visually predictable.
    # lexsort on (x, y) is stable, so ties keep the lowest original index,
    # matching min() over (first.y, first.x).
    idx = int(np.lexsort((starts[:, 0], starts[:, 1]))[0])
    taken = np.zeros(n, dtype=bool)
    taken[idx] = True
    out: Paths = [paths[idx]]
    cx, cy = float(ends[idx, 0]), float(ends[idx, 1])
    for _ in range(n - 1):
        dx0 = starts[:, 0] - cx
        dy0 = starts[:, 1] - cy
        dx1 = ends[:, 0] - cx
        dy1 = ends[:, 1] - cy
        d0 = dx0 * dx0
        d0 = d0 + dy0 * dy0
        d1 = dx1 * dx1
        d1 = d1 + dy1 * dy1
        cand = np.empty((n, 2), dtype=np.float64)
        cand[:, 0] = d0
        cand[:, 1] = d1
        cand[taken] = np.inf
        flat = int(cand.argmin())
        i, rev = flat >> 1, bool(flat & 1)
        taken[i] = True
        p = paths[i]
        if rev:
            p = p[::-1]
            cx, cy = float(starts[i, 0]), float(starts[i, 1])
        else:
            cx, cy = float(ends[i, 0]), float(ends[i, 1])
        out.append(p)
    return out
