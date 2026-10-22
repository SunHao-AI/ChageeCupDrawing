"""白色画布矩形识别。

迁移自 `cup_drawbot.py` 的 `detect_canvas_rect`。唯一改动是入口统一接收图像
数组或 PIL 图像，这样调用方既可以直接传 Maa 截图，也可以传测试用例里的图片。
"""

from __future__ import annotations

import numpy as np
from PIL import Image


def _as_pil(image: Image.Image | np.ndarray) -> Image.Image:
    if isinstance(image, Image.Image):
        return image
    arr = np.asarray(image)
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    if arr.shape[2] == 4:
        return Image.fromarray(arr, mode="RGBA")
    return Image.fromarray(arr[:, :, :3], mode="RGB")


def detect_canvas_rect(image: Image.Image | np.ndarray) -> tuple[int, int, int, int] | None:
    rgb = _as_pil(image).convert("RGB")
    ow, oh = rgb.size
    if ow < 150 or oh < 250:
        return None

    scale = min(1.0, 420 / ow, 720 / oh)
    sw = max(80, int(ow * scale))
    sh = max(120, int(oh * scale))
    small = rgb.resize((sw, sh), Image.Resampling.BILINEAR)
    px = small.load()

    mask = bytearray(sw * sh)
    for y in range(sh):
        base_i = y * sw
        for x in range(sw):
            r, g, b = px[x, y]
            if r >= 250 and g >= 250 and b >= 250 and max(r, g, b) - min(r, g, b) <= 5:
                mask[base_i + x] = 1

    visited = bytearray(sw * sh)
    best = None
    best_score = -1.0

    for sy in range(1, sh - 1):
        for sx in range(1, sw - 1):
            idx = sy * sw + sx
            if not mask[idx] or visited[idx]:
                continue
            stack = [idx]
            visited[idx] = 1
            count = 0
            minx = maxx = sx
            miny = maxy = sy
            touches_border = False
            while stack:
                cur = stack.pop()
                y, x = divmod(cur, sw)
                count += 1
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                if x <= 1 or y <= 1 or x >= sw - 2 or y >= sh - 2:
                    touches_border = True
                for n in (cur - 1 if x > 0 else -1, cur + 1 if x + 1 < sw else -1,
                          cur - sw if y > 0 else -1, cur + sw if y + 1 < sh else -1):
                    if n >= 0 and mask[n] and not visited[n]:
                        visited[n] = 1
                        stack.append(n)

            bw, bh = maxx - minx + 1, maxy - miny + 1
            if touches_border or bw < sw * 0.42 or bh < sh * 0.34 or bw > sw * 0.92 or bh > sh * 0.82:
                continue
            ratio = bw / max(1, bh)
            if not 0.54 <= ratio <= 0.78:
                continue
            fill = count / max(1, bw * bh)
            if fill < 0.63:
                continue
            cx, cy = (minx + maxx) / 2 / sw, (miny + maxy) / 2 / sh
            center_score = max(0.15, 1 - abs(cx - 0.5) * 2) * max(0.15, 1 - abs(cy - 0.47) * 1.2)
            ratio_score = max(0.2, 1 - abs(ratio - 2 / 3) * 3)
            score = count * fill * center_score * ratio_score
            if score > best_score:
                best_score = score
                best = (minx, miny, maxx + 1, maxy + 1)

    if best is None:
        return None
    l, t, r, b = best
    inv = 1.0 / scale
    l, t, r, b = (int(round(v * inv)) for v in (l, t, r, b))
    inset = max(3, int(round(min(ow, oh) * 0.006)))
    return l + inset, t + inset, r - inset, b - inset
