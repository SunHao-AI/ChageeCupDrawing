"""ink_measure 单元测试：合成测试线验证测宽与占用率。"""

from __future__ import annotations

import numpy as np

from app.executor.ink_measure import ink_occupied_fraction, measure_ink_width


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
