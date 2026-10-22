"""拓扑保真填充：同心等距回填 + 漏点补偿。

迁移自 `cup_drawbot.py` 的 `concentric_fill_paths` 与 `topology_rescue_paths`。
两段式设计保留原样：先在距离场等值线上做同心回填，再只对仍然漏掉的像素附近
补骨架路径，这是 V6 召回率能稳定超过 0.98 的原因。
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from app.core.pathing.centerline import greedy_skeleton_paths, morphological_skeleton, zhang_suen_thinning
from app.core.pathing.geometry import binary_to_mask, simplify_polyline
from app.core.pathing.render import render_paths
from app.core.pathing.types import Paths
from app.core.planning.metrics import quality_metrics


def concentric_fill_paths(
    mask: np.ndarray,
    brush_width: float,
    spacing_factor: float,
    simplify_px: float,
    thin_finish: bool = True,
) -> Paths:
    """Pen-width-aware solid fill using signed-distance-like iso-contours.

    This follows the same general plotter principle used by modern pen-fill tools:
    place the pen centre inside the silhouette, then repeatedly inset by roughly one
    pen width. Unlike tracing only the outside contour, filled letters stay filled.
    """
    if cv2.countNonZero(mask) == 0:
        return []
    bw = max(1.0, float(brush_width))
    radius = bw / 2.0
    pitch = max(1.0, bw * max(0.62, min(1.10, float(spacing_factor))))
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    max_dist = float(dist.max())
    paths: Paths = []

    # Start slightly inside half a pen-width. 0.82 empirically balances boundary
    # coverage and spill for WeChat's ~3 px brush measured from the user's sample.
    level = radius * 0.82
    while level <= max_dist + 0.15:
        core = ((dist >= level).astype(np.uint8) * 255)
        contours, _ = cv2.findContours(core, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for c in contours:
            if len(c) < 2 or cv2.arcLength(c, True) < max(2.0, bw * 0.55):
                continue
            eps = max(0.12, float(simplify_px))
            a = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
            if len(a) >= 2:
                p = [(float(x), float(y)) for x, y in a]
                p.append(p[0])
                paths.append(p)
        level += pitch

    if thin_finish:
        # Regions thinner than roughly one pen width may never produce an inset ring.
        # Add a single centreline only for those components. This recovers tiny text
        # and hairline details without turning every thick region into a black blob.
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        thin = np.zeros_like(mask)
        threshold_dist = radius * 1.18
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] <= 1:
                continue
            comp = labels == i
            if float(dist[comp].max()) < threshold_dist:
                thin[comp] = 255
        if cv2.countNonZero(thin):
            skeleton = zhang_suen_thinning(thin)
            raw = greedy_skeleton_paths(skeleton)
            for p in raw:
                if len(p) < 2:
                    continue
                q = simplify_polyline(p, max(0.12, float(simplify_px) * 0.70), False)
                if q:
                    paths.append(q)
    return paths


def topology_rescue_paths(
    target_mask: np.ndarray,
    base_paths: Paths,
    brush_width: int,
    simplify_px: float,
    target_recall: float = 0.985,
) -> tuple[Paths, float, float, float]:
    """Add only the skeleton pieces that explain currently missed target pixels.

    v5's fill paths matched filled shapes well, but a thick handwritten character can
    contain a thin branch that never becomes its own inset contour.  Tracing the *whole*
    skeleton fixes topology but adds too much ink.  v6 instead:

      1. renders the base fill;
      2. finds missed target pixels;
      3. looks for original skeleton pixels within 1 px of those misses;
      4. only accepts skeleton centres that have enough ink around them for the brush.

    The containment threshold is relaxed only as needed to reach ``target_recall``.
    This is an error-correction pass, not a second full tracing pass.
    """
    w = int(target_mask.shape[1])
    h = int(target_mask.shape[0])
    target_img = Image.fromarray(np.where(target_mask > 0, 0, 255).astype(np.uint8), mode="L")
    base_sim = render_paths((w, h), base_paths, int(brush_width))
    p0, r0, i0 = quality_metrics(target_img, base_sim)
    goal = max(0.90, min(0.999, float(target_recall)))
    if r0 >= goal or cv2.countNonZero(target_mask) == 0:
        return [], p0, r0, i0

    rendered = binary_to_mask(base_sim)
    miss = cv2.bitwise_and(target_mask, cv2.bitwise_not(rendered))
    # We only want topology near a real omission, not the entire skeleton.
    near_miss = cv2.dilate(miss, np.ones((3, 3), np.uint8), iterations=1)
    # Zhang-Suen preserves junction topology; the morphological skeleton catches
    # a few diagonal/antialiased pixels the former may omit.  The union is only
    # used near measured misses, so it improves recall without globally doubling lines.
    skel = cv2.bitwise_or(zhang_suen_thinning(target_mask), morphological_skeleton(target_mask))
    dist = cv2.distanceTransform(target_mask, cv2.DIST_L2, 5)

    best_paths: Paths = []
    best_metrics = (p0, r0, i0)
    best_score = i0
    bw = max(1.0, float(brush_width))

    # Conservative -> aggressive.  For the supplied artwork with a 3 px brush,
    # factor 0.52 lifts simulated recall from ~97% to ~98.7% while preserving ~99%
    # precision.  Lower factors are used only when the requested recall needs them.
    for factor in (0.60, 0.54, 0.50, 0.46, 0.42, 0.38):
        min_dist = max(1.0, bw * factor)
        candidate = ((skel > 0) & (near_miss > 0) & (dist >= min_dist)).astype(np.uint8) * 255
        if cv2.countNonZero(candidate) == 0:
            continue
        raw = greedy_skeleton_paths(candidate)
        extra = [
            simplify_polyline(p, max(0.08, float(simplify_px) * 0.45), False)
            for p in raw if p
        ]
        extra = [p for p in extra if p]
        if not extra:
            continue
        trial = base_paths + extra
        sim = render_paths((w, h), trial, int(brush_width))
        pp, rr, ii = quality_metrics(target_img, sim)
        # Prefer the smallest correction that reaches the requested recall. Before
        # reaching it, keep the best IoU/recall candidate seen so the caller still
        # gets a useful plan for physically impossible targets.
        score = ii + min(rr, goal) * 0.02
        if score > best_score:
            best_score = score
            best_paths = extra
            best_metrics = (pp, rr, ii)
        if rr >= goal:
            return extra, pp, rr, ii

    return best_paths, *best_metrics
