"""灰度化、缩放与掩膜降采样。

迁移自 `cup_drawbot.py` 的 `load_grayscale` / `fit_image` / `prepare_base` /
`trim_white_border` / `_resize_mask_to_target` / `_preserve_micro_components`。
函数体保持逐行一致，只把 PIL / OpenCV 的类型注解显式化。
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

# V6 的两种适配模式。中文名保留为别名，前端与预设统一使用英文键。
FIT_CONTAIN = "contain"
FIT_COVER = "cover"

_FIT_ALIASES = {
    "contain": FIT_CONTAIN,
    "完整显示（留白）": FIT_CONTAIN,
    "完整显示": FIT_CONTAIN,
    "cover": FIT_COVER,
    "铺满画布（裁剪）": FIT_COVER,
    "铺满画布": FIT_COVER,
}


def normalize_fit_mode(fit_mode: str) -> str:
    return _FIT_ALIASES.get(str(fit_mode).strip(), FIT_CONTAIN)


def load_grayscale(path: str | Path) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(path))
    # Composite transparency onto white instead of turning transparent pixels black.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, "white")
        bg.alpha_composite(rgba)
        img = bg.convert("RGB")
    return img.convert("L")


def fit_image(img: Image.Image, target_w: int, target_h: int, fit_mode: str) -> Image.Image:
    target_w = max(8, int(target_w))
    target_h = max(8, int(target_h))
    img = img.copy()

    if normalize_fit_mode(fit_mode) == FIT_COVER:
        scale = max(target_w / img.width, target_h / img.height)
        nw = max(1, round(img.width * scale))
        nh = max(1, round(img.height * scale))
        resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = max(0, (nw - target_w) // 2)
        top = max(0, (nh - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    # ``thumbnail()`` never enlarges small images. For a cup sticker that is a bad
    # default: small source artwork would stay physically tiny and its fine text would
    # become impossible for the fixed-width brush. Scale both up and down as needed.
    scale = min(target_w / img.width, target_h / img.height)
    nw = max(1, int(round(img.width * scale)))
    nh = max(1, int(round(img.height * scale)))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("L", (target_w, target_h), 255)
    canvas.paste(img, ((target_w - img.width) // 2, (target_h - img.height) // 2))
    return canvas


def prepare_base(img: Image.Image, w: int, h: int, fit_mode: str, contrast: float) -> Image.Image:
    """Legacy-compatible grayscale preparation.

    v5 deliberately keeps this mild. Earlier versions sharpened and autocontrasted
    after resizing, which could erase 1-2 px strokes or turn anti-aliased text into
    jagged blobs. High-fidelity mode below works from a supersampled grayscale image.
    """
    base = fit_image(img, w, h, fit_mode)
    if abs(float(contrast) - 1.0) > 0.001:
        base = ImageEnhance.Contrast(base).enhance(max(0.3, float(contrast)))
    return base


def _resize_mask_to_target(mask: np.ndarray, target_w: int, target_h: int, keep_fraction: float) -> np.ndarray:
    """Coverage-aware downsample.

    A normal resize followed by thresholding can delete a one-pixel stroke. Instead
    we resize *ink coverage* and keep a target pixel when enough supersampled ink
    contributed to it. ``keep_fraction`` around 0.16-0.25 preserves tiny text while
    avoiding excessive thickening.
    """
    cov = cv2.resize(mask.astype(np.float32) / 255.0, (int(target_w), int(target_h)), interpolation=cv2.INTER_AREA)
    return ((cov >= float(keep_fraction)).astype(np.uint8) * 255)


def _preserve_micro_components(work_mask: np.ndarray, target_mask: np.ndarray, ss: int) -> np.ndarray:
    """Guarantee that meaningful tiny source components survive downsampling.

    Supersampling can still map a tiny dot/radical between target pixel centres.  This
    function only restores a component when *none* of its projected pixels survived.
    It is deliberately conservative so isolated compression speckles are not amplified.
    """
    if cv2.countNonZero(work_mask) == 0:
        return target_mask
    out = target_mask.copy()
    n, labels, stats, cents = cv2.connectedComponentsWithStats(work_mask, 8)
    th, tw = out.shape
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        ww = int(stats[i, cv2.CC_STAT_WIDTH])
        hh = int(stats[i, cv2.CC_STAT_HEIGHT])
        # Keep visible punctuation/radicals, reject one-pixel high-res noise.
        if area < max(3, ss) or max(ww, hh) < max(2, ss):
            continue
        x0 = max(0, int(math.floor(stats[i, cv2.CC_STAT_LEFT] / ss)) - 1)
        y0 = max(0, int(math.floor(stats[i, cv2.CC_STAT_TOP] / ss)) - 1)
        x1 = min(tw, int(math.ceil((stats[i, cv2.CC_STAT_LEFT] + ww) / ss)) + 1)
        y1 = min(th, int(math.ceil((stats[i, cv2.CC_STAT_TOP] + hh) / ss)) + 1)
        if x0 >= x1 or y0 >= y1 or cv2.countNonZero(out[y0:y1, x0:x1]) > 0:
            continue
        cx, cy = cents[i]
        tx = max(0, min(tw - 1, int(round(cx / ss))))
        ty = max(0, min(th - 1, int(round(cy / ss))))
        out[ty, tx] = 255
    return out


def trim_white_border(img: Image.Image, cutoff: int = 247, padding_ratio: float = 0.025) -> Image.Image:
    """Trim mostly-white outer margins while keeping a small safety padding."""
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    ys, xs = np.where(arr < int(cutoff))
    if len(xs) == 0:
        return img
    l, r = int(xs.min()), int(xs.max()) + 1
    t, b = int(ys.min()), int(ys.max()) + 1
    # Don't trim when content already nearly touches all edges.
    if (r - l) > img.width * 0.96 and (b - t) > img.height * 0.96:
        return img
    pad = max(2, int(round(max(r - l, b - t) * float(padding_ratio))))
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(img.width, r + pad), min(img.height, b + pad)
    return img.crop((l, t, r, b))
