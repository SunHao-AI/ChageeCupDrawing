"""二值化与超采样。

迁移自 `cup_drawbot.py` 的 `process_image_hq`。原实现是一个长 if/elif 分支，
这里拆成四个模式函数加一个调度入口，控制流与运算顺序保持不变，保证输出掩膜
与 V6 逐位一致。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

import cv2

from app.core.imaging.preprocess import (
    _preserve_micro_components,
    _resize_mask_to_target,
    fit_image,
    trim_white_border,
)

IMAGE_MODE_ADAPTIVE = "adaptive"
IMAGE_MODE_EDGE = "edge"
IMAGE_MODE_DITHER = "dither"
IMAGE_MODE_FIDELITY = "fidelity"
# V6 的第五种模式「阈值黑白」，在 `PlanParams` 中由 `thresholdMode=True` 表达。
IMAGE_MODE_THRESHOLD = "threshold"

_IMAGE_MODE_ALIASES = {
    "adaptive": IMAGE_MODE_ADAPTIVE,
    "自适应文字/手绘": IMAGE_MODE_ADAPTIVE,
    "自适应文字": IMAGE_MODE_ADAPTIVE,
    "edge": IMAGE_MODE_EDGE,
    "边缘线稿": IMAGE_MODE_EDGE,
    "dither": IMAGE_MODE_DITHER,
    "黑白抖动": IMAGE_MODE_DITHER,
    "fidelity": IMAGE_MODE_FIDELITY,
    "原图保真（推荐）": IMAGE_MODE_FIDELITY,
    "原图保真": IMAGE_MODE_FIDELITY,
    "threshold": IMAGE_MODE_THRESHOLD,
    "阈值黑白": IMAGE_MODE_THRESHOLD,
}


def normalize_image_mode(mode: str) -> str:
    return _IMAGE_MODE_ALIASES.get(str(mode).strip(), IMAGE_MODE_FIDELITY)


def _adaptive_mask(gray: np.ndarray, thr: int, ss: int) -> np.ndarray:
    # CLAHE helps uneven screenshots/paper photos; adaptive threshold keeps
    # small characters which global thresholding can lose locally.
    clahe = cv2.createCLAHE(clipLimit=1.35, tileGridSize=(8, 8))
    local = clahe.apply(gray)
    block = max(15, (31 * ss) | 1)
    mask = cv2.adaptiveThreshold(
        local, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block, max(2, int((215 - thr) / 8) + 5),
    )
    # Limit adaptive noise to pixels that are at least somewhat dark.
    dark_gate = (gray < min(250, thr + 34)).astype(np.uint8) * 255
    return cv2.bitwise_and(mask, dark_gate)


def _edge_mask(gray: np.ndarray, thr: int) -> np.ndarray:
    lo = max(10, int(thr * 0.28))
    hi = max(lo + 10, int(thr * 0.72))
    mask = cv2.Canny(gray, lo, hi, L2gradient=True)
    return cv2.dilate(mask, np.ones((2, 2), np.uint8), iterations=1)


def _dither_mask(gray: np.ndarray) -> np.ndarray:
    pil = Image.fromarray(gray, mode="L").convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    return (np.asarray(pil) < 128).astype(np.uint8) * 255


def _fidelity_mask(gray: np.ndarray, thr: int) -> np.ndarray:
    dark = (gray < thr).astype(np.uint8) * 255
    # Preserve anti-aliased edge samples without globally lowering the
    # threshold. A light pixel is accepted only when adjacent to real ink.
    soft_thr = min(250, thr + 28)
    soft = (gray < soft_thr).astype(np.uint8) * 255
    near_ink = cv2.dilate(dark, np.ones((3, 3), np.uint8), iterations=1)
    return cv2.bitwise_and(soft, near_ink)


def _threshold_mask(gray: np.ndarray, thr: int) -> np.ndarray:
    return (gray < thr).astype(np.uint8) * 255


def process_image_hq(
    img: Image.Image,
    target_w: int,
    target_h: int,
    mode: str,
    threshold: int,
    contrast: float,
    fit_mode: str,
    line_expand: int,
    supersample: int,
    detail_keep: float,
    auto_trim: bool,
    threshold_mode: bool = False,
    return_work_gray: bool = False,
) -> tuple[Image.Image, np.ndarray, int] | tuple[Image.Image, np.ndarray, int, np.ndarray]:
    """Create a high-resolution ink mask and a target-size preview.

    Returns ``(target_binary_pil, work_mask, supersample)`` where work_mask uses
    255=ink and lives at ``target_size * supersample``. All vector planning happens
    at this larger size and coordinates are scaled down only at the end. This is the
    main v5 anti-alias/detail-preservation change. With ``return_work_gray`` the
    work-resolution grayscale (after trim/fit/contrast) is appended for tone
    contour planning.
    """
    ss = max(1, min(4, int(supersample)))
    ww, wh = max(16, int(target_w) * ss), max(24, int(target_h) * ss)
    source = trim_white_border(img) if auto_trim else img
    base = fit_image(source, ww, wh, fit_mode)
    gray = np.asarray(base, dtype=np.uint8)

    # Apply contrast in float space without histogram clipping. This keeps subtle
    # anti-aliased edge pixels which are critical for tiny Chinese/Latin text.
    c = max(0.35, float(contrast))
    if abs(c - 1.0) > 1e-3:
        g = 128.0 + (gray.astype(np.float32) - 128.0) * c
        gray = np.clip(g, 0, 255).astype(np.uint8)

    thr = max(30, min(245, int(threshold)))
    key = IMAGE_MODE_THRESHOLD if threshold_mode else normalize_image_mode(mode)
    if key == IMAGE_MODE_ADAPTIVE:
        mask = _adaptive_mask(gray, thr, ss)
    elif key == IMAGE_MODE_EDGE:
        mask = _edge_mask(gray, thr)
    elif key == IMAGE_MODE_DITHER:
        mask = _dither_mask(gray)
    elif key == IMAGE_MODE_FIDELITY:
        mask = _fidelity_mask(gray, thr)
    else:
        mask = _threshold_mask(gray, thr)

    # Adaptive/photo input may contain broken strokes, so reconnect very small gaps.
    # Clean source artwork deliberately skips this close: closing can fuse nearby
    # handwritten radicals and erase intentional white counters in small characters.
    if key == IMAGE_MODE_ADAPTIVE and ss >= 2:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    expand = max(-2, min(3, int(line_expand)))
    if expand != 0:
        radius = abs(expand) * ss
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
        mask = cv2.dilate(mask, k) if expand > 0 else cv2.erode(mask, k)

    keep = max(0.05, min(0.60, float(detail_keep)))
    target_mask = _resize_mask_to_target(mask, target_w, target_h, keep)
    target_mask = _preserve_micro_components(mask, target_mask, ss)
    target_pil = Image.fromarray(np.where(target_mask > 0, 0, 255).astype(np.uint8), mode="L")
    if return_work_gray:
        return target_pil, mask, ss, gray
    return target_pil, mask, ss
