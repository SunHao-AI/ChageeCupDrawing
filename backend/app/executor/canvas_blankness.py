"""统一画布空白判定（docs/clear-and-guide-design.md §3）。

两层判据，宽严有序：

* L1 自适应背景阈值：以画布区域亮度 P90 估计背景底色，暗于「背景 − delta」
  的像素计墨。白底等价旧固定阈值，带底色/水印画布自动跟随背景。
* L2 空白基线掩码：首次 L1 判空白时快照下采样墨迹掩码存进校准记录，之后
  常驻背景（水印、纸张纹理）不再计入新增墨；只有相对基线的新增超过容差才
  判有墨。

无论走哪层，自适应墨量都必须低于绝对封顶（BLANK_ABS_CEILING）——真空白画布
绝无那么多墨像素，该封顶兜住「基线学到脏画布后永远判空白」的恶性循环，并
由 baseline_is_stale 触发作废重学。

本模块只做纯计算；基线的学习、持久化与失效重学由执行器侧编排。
"""

from __future__ import annotations

import base64

import cv2
import numpy as np

from app.executor.ink_measure import INK_THRESHOLD

BLANK_DELTA = 60  # 相对自适应背景的墨色深度差
BLANK_MIN_PIXELS = 64  # 任何可辨认笔迹都远多于该像素数
BLANK_ABS_CEILING = 256  # 基线豁免路径的固定阈值墨量封顶（防基线学入浓墨）
BASELINE_SCALE = 8  # 基线掩码下采样倍率（投屏画布 ~1.6 KB）
BASELINE_STALE_FLIP = 0.5  # 当前掩码与基线翻转占比超此值判基线失效


def _gray(image: np.ndarray) -> np.ndarray:
    return image[..., 0] if image.ndim == 3 else image


def _crop(gray: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = (int(v) for v in region)
    x2 = min(gray.shape[1], max(0, x) + max(1, w))
    y2 = min(gray.shape[0], max(0, y) + max(1, h))
    x, y = max(0, x), max(0, y)
    if x2 <= x or y2 <= y:
        return np.empty((0, 0), dtype=gray.dtype)
    return gray[y:y2, x:x2]


def _zero_excluded(
    mask: np.ndarray,
    region: tuple[int, int, int, int],
    exclude: "tuple[tuple[int, int, int, int], ...] | None",
) -> np.ndarray:
    """把画布坐标系的豁免矩形（常驻悬浮控件）在墨迹掩码中挖掉（原地修改）。

    豁免矩形由校准里的撤销按钮坐标推出，宁小勿大：挖多了会把用户真笔画豁免
    掉（盲点探测可能误触清空类控件），挖少了只是多一次人工确认。
    """
    if not exclude or mask.size == 0:
        return mask
    rx, ry = int(region[0]), int(region[1])
    for x, y, w, h in exclude:
        x0 = max(0, int(x) - rx)
        y0 = max(0, int(y) - ry)
        x1 = min(mask.shape[1], int(x) - rx + int(w))
        y1 = min(mask.shape[0], int(y) - ry + int(h))
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = False
    return mask


def blank_ink_pixels(
    image: np.ndarray,
    region: tuple[int, int, int, int],
    *,
    delta: int = BLANK_DELTA,
    exclude: "tuple[tuple[int, int, int, int], ...] | None" = None,
) -> int:
    """region 内相对自适应背景的墨像素数（L1 判据的绝对量形式）。"""
    crop = _crop(_gray(image), region)
    if crop.size == 0:
        return 0
    background = np.percentile(crop, 90)
    mask = crop < background - delta
    return int(_zero_excluded(mask, region, exclude).sum())


def _ink_mask(crop: np.ndarray) -> np.ndarray:
    if crop.size == 0:
        return np.zeros((0, 0), dtype=bool)
    background = np.percentile(crop, 90)
    return crop < background - BLANK_DELTA


def _small_mask(mask: np.ndarray) -> np.ndarray:
    height = max(1, mask.shape[0] // BASELINE_SCALE)
    width = max(1, mask.shape[1] // BASELINE_SCALE)
    small = cv2.resize(
        mask.astype(np.uint8) * 255, (width, height), interpolation=cv2.INTER_AREA
    )
    return small > 0  # 面积均值 >0：任一墨像素即置位，宁可多计墨不误判空白


def learn_baseline(
    image: np.ndarray,
    region: tuple[int, int, int, int],
    exclude: "tuple[tuple[int, int, int, int], ...] | None" = None,
) -> dict:
    """把当前帧的墨迹掩码编码为可入库的基线 JSON。"""
    mask = _small_mask(_zero_excluded(_ink_mask(_crop(_gray(image), region)), region, exclude))
    payload = base64.b64encode(mask.astype(np.uint8).tobytes()).decode("ascii")
    return {"w": int(mask.shape[1]), "h": int(mask.shape[0]), "mask": payload}


def _decode_baseline(baseline: dict) -> np.ndarray | None:
    try:
        width, height = int(baseline["w"]), int(baseline["h"])
        raw = base64.b64decode(baseline["mask"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or len(raw) != width * height:
        return None
    return np.frombuffer(raw, dtype=np.uint8).reshape((height, width)) > 0


def _aligned(baseline_mask: np.ndarray, target_shape: tuple[int, int]) -> np.ndarray:
    if baseline_mask.shape == target_shape:
        return baseline_mask
    resized = cv2.resize(
        baseline_mask.astype(np.uint8) * 255,
        (target_shape[1], target_shape[0]),
        interpolation=cv2.INTER_NEAREST,
    )
    return resized > 0


def is_blank(
    image: np.ndarray,
    region: tuple[int, int, int, int],
    baseline: dict | None,
    exclude: "tuple[tuple[int, int, int, int], ...] | None" = None,
) -> bool:
    """统一空白判定：L1 直判；或 L2 基线比对放行（受固定阈值墨量封顶约束）。

    ``exclude`` 是画布坐标系下的豁免矩形（常驻悬浮控件，如悬浮撤销按钮），
    所有墨量计数前先从掩码中挖掉。
    """
    adaptive = blank_ink_pixels(image, region, exclude=exclude)
    if adaptive < BLANK_MIN_PIXELS:
        return True
    baseline_mask = _decode_baseline(baseline) if baseline else None
    if baseline_mask is None:
        return False
    # 封顶用固定阈值墨量：浓黑污迹学进基线后也要被拦住；浅色常驻背景
    # （水印、纸纹）不进固定计数，由基线正常豁免——白底画布是画布检测的
    # 前提，深黑大面积背景走不到这里。
    crop = _crop(_gray(image), region)
    if crop.size:
        fixed = _zero_excluded(
            crop < INK_THRESHOLD, region, exclude
        ).sum()
        if int(fixed) >= BLANK_ABS_CEILING:
            return False
    current = _small_mask(_zero_excluded(
        _ink_mask(_crop(_gray(image), region)), region, exclude))
    aligned = _aligned(baseline_mask, current.shape)
    added = int((current & ~aligned).sum())
    # 下采样后每格对应 SCALE² 个大像素；容差换算到格数。
    return added * (BASELINE_SCALE * BASELINE_SCALE) < BLANK_MIN_PIXELS


def baseline_is_stale(
    image: np.ndarray,
    region: tuple[int, int, int, int],
    baseline: dict | None,
    exclude: "tuple[tuple[int, int, int, int], ...] | None" = None,
) -> bool:
    """当前掩码与基线整幅翻转占比过高（底图更换）→ 基线应作废重学。"""
    baseline_mask = _decode_baseline(baseline) if baseline else None
    if baseline_mask is None:
        return False
    current = _small_mask(_zero_excluded(
        _ink_mask(_crop(_gray(image), region)), region, exclude))
    aligned = _aligned(baseline_mask, current.shape)
    flipped = (current ^ aligned).mean() if current.size else 0.0
    return bool(flipped > BASELINE_STALE_FLIP)
