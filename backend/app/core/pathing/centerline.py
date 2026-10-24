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

    For speed the work is cropped to the content bbox plus a 1 px background
    ring, and each sub-iteration only re-evaluates pixels whose 8-neighbourhood
    changed since the previous scan *of the same sub-iteration* (a pixel with
    an unchanged neighbourhood has an unchanged removal condition, so freezing
    it is provably identity-preserving).  The result is bit-identical to the
    naïve full-array scan.
    """
    img = (mask > 0).astype(np.uint8)
    if img.ndim != 2:
        raise ValueError("thinning expects a 2D mask")
    if img.shape[0] < 3 or img.shape[1] < 3:
        return (img * 255).astype(np.uint8)

    rows = np.flatnonzero(img.any(axis=1))
    if rows.size == 0:
        return np.zeros(img.shape, dtype=np.uint8)
    cols = np.flatnonzero(img.any(axis=0))
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    sub = np.pad(img[y0:y1, x0:x1], 1)
    h, w = sub.shape
    # Centre region: inside the 1 px ring, and matching the full-array scan
    # which never removes pixels from the outermost row/column.
    top = 1 if y0 > 0 else 2
    left = 1 if x0 > 0 else 2
    bottom = h - 1 if y1 < img.shape[0] else h - 2
    right = w - 1 if x1 < img.shape[1] else w - 2
    valid = np.zeros((h, w), dtype=bool)
    valid[top:bottom, left:right] = True

    flat = sub.reshape(-1)
    kernel = np.ones((3, 3), dtype=np.uint8)
    # Per-step scan state: `needs_full` marks a step whose first scan must
    # cover every centre pixel; `acc` is a marker of removals since that
    # step's previous scan (its 3x3 dilation is exactly the set of pixels
    # whose neighbourhood changed).
    needs_full = [True, True]
    acc: list[np.ndarray | None] = [None, None]
    for _ in range(max(1, int(max_iters))):
        changed = False
        for step in (0, 1):
            if needs_full[step]:
                needs_full[step] = False
                cand2d = valid
            elif acc[step] is None:
                continue
            else:
                cand2d = cv2.dilate(acc[step], kernel, dst=None).astype(bool) & valid
                acc[step] = None
            idx = np.flatnonzero(cand2d.reshape(-1))
            if idx.size == 0:
                continue
            p1 = flat[idx]
            p2 = flat[idx - w]
            p3 = flat[idx - w + 1]
            p4 = flat[idx + 1]
            p5 = flat[idx + w + 1]
            p6 = flat[idx + w]
            p7 = flat[idx + w - 1]
            p8 = flat[idx - 1]
            p9 = flat[idx - w - 1]

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
            remove = (p1 == 1) & (b >= 2) & (b <= 6) & (a == 1)
            if step == 0:
                remove &= (p2 * p4 * p6) == 0
                remove &= (p4 * p6 * p8) == 0
            else:
                remove &= (p2 * p4 * p8) == 0
                remove &= (p2 * p6 * p8) == 0
            sel = idx[remove]
            if sel.size:
                flat[sel] = 0
                changed = True
                for other in (0, 1):
                    if acc[other] is None:
                        acc[other] = np.zeros((h, w), dtype=np.uint8)
                    acc[other].reshape(-1)[sel] = 1
        if not changed:
            break

    out = np.zeros(img.shape, dtype=np.uint8)
    out[y0:y1, x0:x1] = sub[1:-1, 1:-1]
    return (out * 255).astype(np.uint8)


def morphological_skeleton(mask: np.ndarray) -> np.ndarray:
    bbox = cv2.boundingRect((mask > 0).astype(np.uint8))
    if bbox[2] == 0 or bbox[3] == 0:
        return np.zeros(mask.shape, dtype=mask.dtype)
    x, y, w, h = bbox
    x0, y0 = max(x - 1, 0), max(y - 1, 0)
    # 1 px background ring: erode/dilate borders read it exactly like the
    # full-array background, so the skeleton is identical to the uncropped run.
    sub = mask[y0:y + h + (1 if y + h < mask.shape[0] else 0),
               x0:x + w + (1 if x + w < mask.shape[1] else 0)]
    skel_sub = _morphological_skeleton_core(sub)
    skel = np.zeros(mask.shape, dtype=skel_sub.dtype)
    skel[y0:y0 + skel_sub.shape[0], x0:x0 + skel_sub.shape[1]] = skel_sub
    return skel


def _morphological_skeleton_core(img: np.ndarray) -> np.ndarray:
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
