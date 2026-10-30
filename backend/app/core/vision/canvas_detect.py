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


def detect_canvas_rect(
    image: Image.Image | np.ndarray, max_ratio: float = 0.78
) -> tuple[int, int, int, int] | None:
    """识别白色画布矩形。

    ``max_ratio`` 是宽/高比上限，默认 0.78 与 v6 参考实现严格对齐（golden
    parity）。工具栏探测器在面板展开态重建画布时放宽到 0.85：画布变矮近方
    形（真机实测 ratio≈0.80），默认档直接拒检会让探测器拿旧收起态矩形继续
    工作、工具行/胶囊全部错位。
    """
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
            if not 0.54 <= ratio <= max_ratio:
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
    l, t, r, b = _trim_at_full_width_seam(_as_pil(image).convert("L"), (l, t, r, b))
    inset = max(3, int(round(min(ow, oh) * 0.006)))
    return l + inset, t + inset, r - inset, b - inset


def _trim_at_full_width_seam(
    gray: Image.Image, rect: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """在候选矩形内切断「全宽非白分隔带」，把底缘收回第一道带之上。

    画板画布卡片底缘有一条全宽阴影分隔带（真机灰度 249）。面板展开态下画布
    白区与面板/底部白条仅隔着这条带，白判据（各通道 ≥250）漏孔 → 连通域把
    画布与下方白色并成一个高矩形，画布底缘被抬到面板之下，工具条定带全部
    错位。必须在**全分辨率**灰度上找缝：带只有几像素高、灰度只比白判据低
    1~6 级，缩小图的双线性插值会把它洗成白行（真机实测 420 宽档漏检）。

    分隔带不会出现在画布内部：画布内容（用户笔画/探测线）只占局部宽度，
    够不到「整行 85% 非白」；跳过顶部 20% 留白防画布内深色横幅误切。
    """
    l, t, r, b = rect
    height = b - t
    if height < 4:
        return rect
    arr = np.asarray(gray, dtype=np.uint8)
    x0, x1 = min(arr.shape[1], l), max(l + 1, min(arr.shape[1], r))
    if x1 - x0 < 8:
        return rect
    # 上界 20% 防画布内深色横幅误切；下界放到候选底缘：卡片缝贴底缘仅 2~13px
    # （收起态相对候选高度 ~98.4%），更早的截断切不到 → 收起态被多吸 ~29px，
    # 与校准值假性漂移 3.6% → E_CANVAS_DRIFT 误报。候选底缘行本身是连通域内
    # 白像素（mask 判据比 <250 更严），不会自切。
    limit = b
    for y in range(t + max(1, int(height * 0.20)), limit):
        row = arr[y, x0:x1]
        if float((row < 250).mean()) >= 0.85:
            return (l, t, r, y)
    return rect
