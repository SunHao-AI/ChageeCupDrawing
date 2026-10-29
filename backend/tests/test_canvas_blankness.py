"""统一空白判定单测（clear-and-guide-design §3）。"""

from __future__ import annotations

import numpy as np

from app.executor.canvas_blankness import (
    BASELINE_SCALE,
    BLANK_ABS_CEILING,
    BLANK_DELTA,
    BLANK_MIN_PIXELS,
    baseline_is_stale,
    blank_ink_pixels,
    is_blank,
    learn_baseline,
)

REGION = (0, 0, 400, 300)


def _blank_canvas(background: int = 250) -> np.ndarray:
    return np.full((300, 400, 3), background, dtype=np.uint8)


def _with_ink(lines: int, background: int = 250) -> np.ndarray:
    """每行 46×3 墨线（与探测测试线同尺寸），约 138 px/行。"""
    image = _blank_canvas(background)
    for i in range(lines):
        image[20 + i * 8 : 23 + i * 8, 20:66] = 30
    return image


def _with_texture(background: int = 235) -> np.ndarray:
    """满布纹理（灰度 155~174）的"纸纹画布"：大量像素跨旧固定阈值 160，
    固定阈值判有墨；自适应 P90（≈173−60）判空白。"""
    image = np.full((300, 400, 3), background, dtype=np.uint8)
    noise = np.random.default_rng(7).integers(0, 20, size=(300, 400), dtype=np.uint8)
    gray = image[..., 0]
    gray[...] = np.clip(background - 80 + noise.astype(int), 0, 255).astype(np.uint8)
    return np.dstack([gray, gray, gray])


def test_blank_canvas_is_blank() -> None:
    assert is_blank(_blank_canvas(), REGION, None)
    assert blank_ink_pixels(_blank_canvas(), REGION) == 0


def test_textured_background_blank_under_adaptive_threshold() -> None:
    textured = _with_texture()
    # 固定阈值下满布"墨"；自适应 P90 判空白。
    assert int((textured[..., 0] < 160).sum()) > BLANK_ABS_CEILING
    assert is_blank(textured, REGION, None)


def test_ink_lines_detected_without_baseline() -> None:
    # 一行 138 px 的墨线低于旧判据吗？不：138 ≥ 64 应判有墨。
    assert not is_blank(_with_ink(1), REGION, None)
    assert not is_blank(_with_ink(3), REGION, None)


def test_adaptive_detects_faint_ink_on_textured_background() -> None:
    textured = _with_texture()
    textured[20:24, 20:80] = textured[..., 0][20, 20] - 120  # 相对背景 −120 的淡墨
    textured = np.dstack([textured[..., 0]] * 3)
    assert blank_ink_pixels(textured, REGION) >= BLANK_MIN_PIXELS
    assert not is_blank(textured, REGION, None)


def test_baseline_learns_watermark_and_tolerates_it() -> None:
    """常驻浅色水印进基线后不再让画布被判有墨。

    水印灰 175：浅于固定阈值 160 不触发封顶（封顶防的是浓黑笔迹学进基线），
    但深于自适应阈值（250−60=190）会让无基线的 L1 判有墨——这正是基线要救的场景。
    """
    with_mark = _blank_canvas()
    with_mark[..., 0][100:220, 100:300] = 175
    assert not is_blank(with_mark, REGION, None)
    baseline = learn_baseline(with_mark, REGION)
    assert is_blank(with_mark, REGION, baseline)
    # 基线之上再加一条新墨线 → 判有墨。
    marked = with_mark.copy()
    marked[20:23, 20:66] = 30
    assert not is_blank(marked, REGION, baseline)


def test_abs_ceiling_beats_poisoned_baseline() -> None:
    """基线学脏（把浓墨学成背景）也不能永远判空白：绝对封顶拦截。"""
    dirty = _with_ink(40)  # 40 行浓墨 ≈ 5520 px
    baseline = learn_baseline(dirty, REGION)  # 恶性路径：脏帧被学成基线
    # 同一脏帧：自适应墨量超绝对封顶 → 即便基线差为 0 也判不空白。
    assert blank_ink_pixels(dirty, REGION) >= BLANK_ABS_CEILING
    assert not is_blank(dirty, REGION, baseline)


def test_baseline_stale_when_persistent_pattern_gone() -> None:
    """常驻图案大面积消失（更换底图/清掉常驻水印）→ 基线作废重学。

    均匀换底色不会翻转掩码（判据是相对自适应背景的），只有基线里置位的
    格子在当前帧不再置位（或反之）且占比过半才算失效。
    """
    old = _blank_canvas()
    clean_baseline = learn_baseline(old, REGION)
    assert not baseline_is_stale(old, REGION, clean_baseline)
    # 均匀深底：自适应背景下仍无墨，掩码不变 → 不算失效。
    assert not baseline_is_stale(_blank_canvas(background=40), REGION, clean_baseline)

    # 大面积常驻水印（灰 175，浅于固定阈值 160，掩码大面积置位）学进基线。
    with_mark = _blank_canvas()
    with_mark[..., 0][10:290, 10:390] = 175
    marked_baseline = learn_baseline(with_mark, REGION)
    assert not baseline_is_stale(with_mark, REGION, marked_baseline)
    # 水印整体消失 → 基线置位格全部翻转 → 判失效。
    assert baseline_is_stale(_blank_canvas(), REGION, marked_baseline)


def test_baseline_roundtrip_and_garbage_tolerance() -> None:
    baseline = learn_baseline(_blank_canvas(), REGION)
    assert baseline["w"] == 400 // BASELINE_SCALE
    assert baseline["h"] == 300 // BASELINE_SCALE
    # 结构非法的基线按"无基线"处理，不抛异常。
    assert not is_blank(_with_ink(10), REGION, {"w": 50, "h": 40, "mask": "!!!"})
    assert is_blank(_blank_canvas(), REGION, {"w": 50, "h": 40, "mask": "!!!"})


def test_delta_covers_screen_noise() -> None:
    """投屏压缩噪点（背景 ±12）不应造出假墨。"""
    image = _blank_canvas(220)
    noise = np.random.default_rng(3).integers(-12, 13, size=(300, 400)).astype(int)
    gray = np.clip(image[..., 0].astype(int) + noise, 0, 255).astype(np.uint8)
    noisy = np.dstack([gray, gray, gray])
    assert blank_ink_pixels(noisy, REGION) < BLANK_MIN_PIXELS
    assert is_blank(noisy, REGION, None)
    assert BLANK_DELTA >= 60
