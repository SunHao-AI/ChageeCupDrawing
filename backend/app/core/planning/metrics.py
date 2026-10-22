"""质量指标与差异图。

迁移自 `cup_drawbot.py` 的 `_quality_metrics` 与 `make_difference_image`。
"""

from __future__ import annotations

import numpy as np
from PIL import Image


def quality_metrics(binary: Image.Image, simulated: Image.Image) -> tuple[float, float, float]:
    a = np.asarray(binary, dtype=np.uint8) < 128
    b = np.asarray(simulated, dtype=np.uint8) < 128
    inter = int(np.logical_and(a, b).sum())
    p = inter / max(1, int(b.sum()))
    r = inter / max(1, int(a.sum()))
    iou = inter / max(1, int(np.logical_or(a, b).sum()))
    return p, r, iou


def make_difference_image(binary: Image.Image, simulated: Image.Image) -> Image.Image:
    """Diagnostic: black=matched ink, red=missed detail, blue=extra ink."""
    a = np.asarray(binary, dtype=np.uint8) < 128
    b = np.asarray(simulated, dtype=np.uint8) < 128
    out = np.full((a.shape[0], a.shape[1], 3), 255, dtype=np.uint8)
    both = a & b
    miss = a & ~b
    spill = b & ~a
    out[both] = (0, 0, 0)
    out[miss] = (220, 40, 40)
    out[spill] = (55, 95, 220)
    return Image.fromarray(out, mode="RGB")
