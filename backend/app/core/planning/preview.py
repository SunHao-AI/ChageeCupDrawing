"""预览渲染：差异图与合成图。

差异图迁移自 V6 `make_difference_image`（黑=命中，红=漏画，蓝=越界）。合成图把
原图按同一次适配参数铺回画布，再叠加模拟笔迹，用于判断「真实观感」。
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops

from app.core.imaging.preprocess import prepare_base


def make_difference_image(binary: Image.Image, simulated: Image.Image) -> Image.Image:
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


def make_composite(
    original: Image.Image,
    simulated: Image.Image,
    *,
    inner_w: int,
    inner_h: int,
    fit_mode: str,
    contrast: float,
    margin: int,
) -> Image.Image:
    """原图 + 模拟笔迹；``simulated`` 为黑笔白底，取较暗值即可叠加。"""
    base = prepare_base(original, int(inner_w), int(inner_h), fit_mode, contrast)
    canvas = Image.new("L", simulated.size, 255)
    canvas.paste(base, (int(margin), int(margin)))
    return ImageChops.darker(canvas, simulated.convert("L"))
