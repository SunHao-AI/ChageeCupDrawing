"""折线渲染：OpenCV 版用于拓扑补偿的快速迭代，Pillow 版用于最终预览与指标。"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw

from app.core.pathing.geometry import path_length
from app.core.pathing.types import Paths


def cv_render_paths(shape: tuple[int, int], paths: Paths, brush_width: float) -> np.ndarray:
    out = np.zeros(shape, dtype=np.uint8)
    bw = max(1, int(round(brush_width)))
    for p in paths:
        if not p:
            continue
        pts = np.asarray(p, dtype=np.float32).round().astype(np.int32).reshape(-1, 1, 2)
        if len(p) == 1:
            x, y = pts[0, 0]
            cv2.circle(out, (int(x), int(y)), max(1, bw // 2), 255, -1, lineType=cv2.LINE_8)
        else:
            cv2.polylines(out, [pts], False, 255, bw, lineType=cv2.LINE_8)
    return out


def render_paths(size: tuple[int, int], paths: Paths, brush_width: int) -> Image.Image:
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    bw = max(1, int(brush_width))
    radius = bw / 2
    for p in paths:
        if not p:
            continue
        if len(p) == 1 or path_length(p) < 1.2:
            x, y = p[0]
            d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        else:
            d.line(p, fill=0, width=bw, joint="curve")
            # Pillow line caps are not always round; add endpoint caps to better mimic a brush.
            for x, y in (p[0], p[-1]):
                d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
    return img
