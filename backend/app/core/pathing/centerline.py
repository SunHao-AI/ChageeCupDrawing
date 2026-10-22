"""中心线策略：Zhang-Suen 细化 + 贪心骨架追踪。"""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.core.pathing.types import Path, Paths


def zhang_suen_thinning(mask: np.ndarray, max_iters: int = 128) -> np.ndarray:
    """Topology-preserving 1 px thinning for handwriting/line art.

    This is a compact Zhang-Suen implementation.  Compared with repeated
    morphology, it is much less likely to delete a short branch at a junction,
    which is exactly the failure mode seen in tiny handwritten Chinese strokes.
    Input/output use OpenCV's 0/255 convention.
    """
    img = (mask > 0).astype(np.uint8)
    if img.ndim != 2:
        raise ValueError("thinning expects a 2D mask")
    if img.shape[0] < 3 or img.shape[1] < 3:
        return (img * 255).astype(np.uint8)

    for _ in range(max(1, int(max_iters))):
        changed = False
        for step in (0, 1):
            p1 = img[1:-1, 1:-1]
            p2 = img[:-2, 1:-1]
            p3 = img[:-2, 2:]
            p4 = img[1:-1, 2:]
            p5 = img[2:, 2:]
            p6 = img[2:, 1:-1]
            p7 = img[2:, :-2]
            p8 = img[1:-1, :-2]
            p9 = img[:-2, :-2]

            b = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            a = (
                ((p2 == 0) & (p3 == 1)).astype(np.uint8)
                + ((p3 == 0) & (p4 == 1)).astype(np.uint8)
                + ((p4 == 0) & (p5 == 1)).astype(np.uint8)
                + ((p5 == 0) & (p6 == 1)).astype(np.uint8)
                + ((p6 == 0) & (p7 == 1)).astype(np.uint8)
                + ((p7 == 0) & (p8 == 1)).astype(np.uint8)
                + ((p8 == 0) & (p9 == 1)).astype(np.uint8)
                + ((p9 == 0) & (p2 == 1)).astype(np.uint8)
            )
            common = (p1 == 1) & (b >= 2) & (b <= 6) & (a == 1)
            if step == 0:
                remove = common & ((p2 * p4 * p6) == 0) & ((p4 * p6 * p8) == 0)
            else:
                remove = common & ((p2 * p4 * p8) == 0) & ((p2 * p6 * p8) == 0)
            if np.any(remove):
                yy, xx = np.where(remove)
                # Work on the full array so the neighbour arrays are refreshed next pass.
                img[yy + 1, xx + 1] = 0
                changed = True
        if not changed:
            break
    return (img * 255).astype(np.uint8)


def morphological_skeleton(mask: np.ndarray) -> np.ndarray:
    img = mask.copy()
    skel = np.zeros_like(img)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    # Typical cup canvas is ~550x830; this loop usually finishes in <20 iterations.
    for _ in range(128):
        eroded = cv2.erode(img, element)
        opened = cv2.dilate(eroded, element)
        residue = cv2.subtract(img, opened)
        skel = cv2.bitwise_or(skel, residue)
        img = eroded
        if cv2.countNonZero(img) == 0:
            break
    return skel


def greedy_skeleton_paths(skeleton: np.ndarray) -> Paths:
    """Cover a 1px skeleton with long-ish strokes.

    At a branch we continue in the straightest direction; remaining branches are
    picked up in later strokes. This avoids the thousands of tiny graph segments
    produced by naïvely splitting at every diagonal junction.
    """
    on = skeleton > 0
    h, w = on.shape
    visited = np.zeros_like(on, dtype=np.bool_)
    offsets = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

    # Degree helps choose endpoints first.
    degree = np.zeros_like(skeleton, dtype=np.uint8)
    for dx, dy in offsets:
        y0s, y1s = max(0, -dy), min(h, h - dy)
        x0s, x1s = max(0, -dx), min(w, w - dx)
        y0d, y1d = max(0, dy), min(h, h + dy)
        x0d, x1d = max(0, dx), min(w, w + dx)
        degree[y0d:y1d, x0d:x1d] += on[y0s:y1s, x0s:x1s]

    remaining = int(on.sum())
    paths: Paths = []
    endpoint_list = [tuple(map(int, p)) for p in np.argwhere(on & (degree <= 1))]
    all_list = [tuple(map(int, p)) for p in np.argwhere(on)]
    ep_i = 0
    all_i = 0

    while remaining > 0:
        while ep_i < len(endpoint_list) and visited[endpoint_list[ep_i][0], endpoint_list[ep_i][1]]:
            ep_i += 1
        if ep_i < len(endpoint_list):
            y, x = endpoint_list[ep_i]
            ep_i += 1
        else:
            while all_i < len(all_list) and visited[all_list[all_i][0], all_list[all_i][1]]:
                all_i += 1
            if all_i >= len(all_list):
                break
            y, x = all_list[all_i]
            all_i += 1

        path: Path = []
        prev_dir: tuple[int, int] | None = None
        while True:
            path.append((float(x), float(y)))
            visited[y, x] = True
            remaining -= 1

            candidates: list[tuple[float, int, int, int, int]] = []
            for dx, dy in offsets:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if not on[ny, nx] or visited[ny, nx]:
                    continue
                if prev_dir is None:
                    score = 0.0
                else:
                    score = (dx * prev_dir[0] + dy * prev_dir[1]) / math.hypot(dx, dy)
                # Mildly prefer simple continuation over entering a dense junction.
                score += 0.025 * (2 - int(degree[ny, nx]))
                candidates.append((score, nx, ny, dx, dy))

            if not candidates:
                break
            candidates.sort(reverse=True)
            _score, nx, ny, dx, dy = candidates[0]
            prev_dir = (dx, dy)
            x, y = nx, ny

        if len(path) >= 2:
            paths.append(path)
        elif path:
            paths.append(path)

    return paths
