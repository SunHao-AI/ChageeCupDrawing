"""ink_measure 单元测试：合成测试线验证测宽、占用率与画布墨迹差分。"""

from __future__ import annotations

import numpy as np

from app.executor.ink_measure import (
    detect_stray_ink,
    ink_occupied_fraction,
    ink_pixels,
    measure_ink_width,
)


def _canvas(width: int = 200, height: int = 200) -> np.ndarray:
    return np.full((height, width), 255, dtype=np.uint8)


def test_measure_horizontal_line_width() -> None:
    img = _canvas()
    # 5px 粗的水平线（y=90..94），跨 x=20..180。
    img[90:95, 20:180] = 0
    width = measure_ink_width(img, (10, 80, 180, 30))
    assert width is not None
    assert abs(width - 5.0) < 1e-6


def test_measure_broken_line_still_median() -> None:
    img = _canvas()
    img[90:93, 20:180] = 0
    img[90:93, 60:70] = 255  # 断点不影响中位厚度
    width = measure_ink_width(img, (10, 80, 180, 30))
    assert width == 3.0


def test_measure_empty_region_returns_none() -> None:
    img = _canvas()
    assert measure_ink_width(img, (10, 10, 50, 50)) is None


def test_measure_ignores_ink_outside_region() -> None:
    img = _canvas()
    img[150:160, 0:200] = 0  # 区域外的粗线
    assert measure_ink_width(img, (10, 10, 100, 30)) is None


def test_bgr_input_supported() -> None:
    img = np.full((100, 100, 3), 255, dtype=np.uint8)
    img[40:46, 10:90] = 0
    assert measure_ink_width(img, (5, 35, 90, 15)) == 6.0


def test_occupied_fraction() -> None:
    img = _canvas(100, 100)
    assert ink_occupied_fraction(img, (0, 0, 100, 100)) == 0.0
    img[0:10, 0:100] = 0
    assert abs(ink_occupied_fraction(img, (0, 0, 100, 100)) - 0.10) < 1e-9


# ---------- ink_pixels / detect_stray_ink（画布差分守护） ----------

_STROKE = [(20, 100), (180, 100)]
_REGION = (0, 0, 200, 200)


def _stroke_frame() -> tuple[np.ndarray, np.ndarray]:
    before = _canvas()
    after = _canvas()
    after[99:102, 20:180] = 0  # 注入的水平笔迹（3px 粗）
    return before, after


def test_ink_pixels_counts_absolute() -> None:
    img = _canvas(100, 100)
    assert ink_pixels(img, (0, 0, 100, 100)) == 0
    img[0:10, 0:100] = 0
    assert ink_pixels(img, (0, 0, 100, 100)) == 1000


def test_stray_none_when_only_planned_stroke() -> None:
    before, after = _stroke_frame()
    stray, changed = detect_stray_ink(before, after, _STROKE, _REGION)
    assert (stray, changed) == (0, False)


def test_stray_detects_remote_touch() -> None:
    before, after = _stroke_frame()
    after[20:40, 20:40] = 0  # 画布另一角的手指误触（400px）
    stray, changed = detect_stray_ink(before, after, _STROKE, _REGION)
    assert changed is False
    assert stray >= 400


def test_stray_ignores_overlap_near_stroke() -> None:
    before, after = _stroke_frame()
    after[104:112, 50:60] = 0  # 落在笔迹膨胀邻域（±16px）内的重叠触碰
    stray, _ = detect_stray_ink(before, after, _STROKE, _REGION)
    assert stray == 0


def test_stray_ignores_tiny_specks() -> None:
    before, after = _stroke_frame()
    after[20:26, 20:26] = 0  # 36px < min_px=40，视为渲染抖动
    stray, _ = detect_stray_ink(before, after, _STROKE, _REGION)
    assert stray == 0


def test_screen_change_when_majority_flips() -> None:
    before = _canvas()
    after = _canvas()
    after[0:150, :] = 0  # 75% 区域翻转（弹窗覆盖/清空），无论变暗都算整体变化
    stray, changed = detect_stray_ink(before, after, _STROKE, _REGION)
    assert (stray, changed) == (0, True)


def test_stray_no_new_ink() -> None:
    before = _canvas()
    after = _canvas()
    after[99:102, 20:180] = 0
    stray, changed = detect_stray_ink(after, after, _STROKE, _REGION)
    assert (stray, changed) == (0, False)
    assert detect_stray_ink(before, before, _STROKE, _REGION) == (0, False)


def test_stray_empty_region_and_bgr() -> None:
    before, after = _stroke_frame()
    # 退化区域直接放行。
    assert detect_stray_ink(before, after, _STROKE, (300, 300, 10, 10)) == (0, False)
    b3 = np.stack([before] * 3, axis=-1)
    a3 = np.stack([after] * 3, axis=-1)
    a3[20:40, 20:40] = 0
    stray, changed = detect_stray_ink(b3, a3, _STROKE, _REGION)
    assert changed is False and stray >= 400
